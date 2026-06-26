"""Review-fix tests: the --confirmed CONFIRM placement path, and slot release on
non-placement (retryable failures and pending CONFIRMs must not burn the day).
"""
import pytest

from engine.models import DecisionStatus
from providers.base import OrderStatus, ProviderUnavailable
from providers.mock import MockProvider
from run import run


def _confirm_cfg(tmp_path):
    # auto_approve_under=10 -> the happy $14 candidate lands in the CONFIRM band.
    path = tmp_path / "c.yaml"
    path.write_text("budget:\n  daily_max_usd: 25\n  auto_approve_under_usd: 10\n", encoding="utf-8")
    return path


def test_confirm_is_pending_without_flag_and_placed_with_it(tmp_path):
    cfg = _confirm_cfg(tmp_path)
    pending = run(cfg, provider=MockProvider("happy"))
    assert pending.decision.status is DecisionStatus.CONFIRM
    assert pending.placed is False

    approved = run(cfg, provider=MockProvider("happy"), confirmed=True)
    assert approved.decision.status is DecisionStatus.CONFIRM
    assert approved.placed is True
    assert approved.order_result.status is OrderStatus.PLACED


def test_block_never_places_even_with_confirmed(tmp_path):
    cfg = _confirm_cfg(tmp_path)
    result = run(cfg, provider=MockProvider("over_budget"), confirmed=True)
    assert result.decision.status is DecisionStatus.BLOCK
    assert result.placed is False


class _DeadProvider:
    name = "dead"

    def discover(self, config):
        raise ProviderUnavailable("bot wall")

    def place_order(self, *a, **k):  # pragma: no cover
        raise AssertionError


def test_retryable_failure_releases_the_slot(tmp_path):
    cfg = _confirm_cfg(tmp_path)
    slots = tmp_path / "slots"
    with pytest.raises(ProviderUnavailable):
        run(cfg, provider=_DeadProvider(), claim_slot=True, slot_dir=slots)
    # the claim was released, so no marker remains and a retry is not skipped
    assert list(slots.glob("*.json")) == []
    ok = run(cfg, provider=MockProvider("happy"), confirmed=True, claim_slot=True, slot_dir=slots)
    assert ok.already_ran is False
    assert ok.placed is True


def test_pending_confirm_does_not_burn_the_slot(tmp_path):
    cfg = _confirm_cfg(tmp_path)
    slots = tmp_path / "slots"
    pending = run(cfg, provider=MockProvider("happy"), claim_slot=True, slot_dir=slots)
    assert pending.placed is False  # CONFIRM, not placed -> slot released
    approved = run(cfg, provider=MockProvider("happy"), confirmed=True, claim_slot=True, slot_dir=slots)
    assert approved.already_ran is False
    assert approved.placed is True


def test_placed_order_does_consume_the_slot(tmp_path):
    # daily run that AUTO-places should still be idempotent (second run skips).
    cfg = tmp_path / "auto.yaml"
    cfg.write_text("budget:\n  daily_max_usd: 25\n  auto_approve_under_usd: 18\n", encoding="utf-8")
    slots = tmp_path / "slots"
    first = run(cfg, provider=MockProvider("happy"), claim_slot=True, slot_dir=slots)
    assert first.placed is True
    second = run(cfg, provider=MockProvider("happy"), claim_slot=True, slot_dir=slots)
    assert second.already_ran is True
    assert second.placed is False
