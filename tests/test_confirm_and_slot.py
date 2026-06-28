"""Review-fix tests: the --confirmed CONFIRM placement path, and slot release on
non-placement (retryable failures and pending CONFIRMs must not burn the day).
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from engine.models import Candidate, DecisionStatus
from providers.base import OrderResult, OrderStatus, ProviderUnavailable
from providers.mock import MockProvider
from run import run, _try_claim_slot, _CLAIM_STALE_SECONDS


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


class _StopProvider:
    name = "stop"

    def discover(self, config):
        return [Candidate("Real Store", "Real Dish", 12, verified_safe=True)]

    def place_order(self, candidate, *, idempotency_key, complete_payment=False,
                    budget_ceiling_usd=None, auto_approve_ceiling_usd=None, clear_cart=False):
        return OrderResult(
            status=OrderStatus.STOPPED_BEFORE_PAYMENT, provider="stop",
            restaurant=candidate.restaurant, item_name=candidate.item_name,
            price_usd=candidate.price_usd, idempotency_key=idempotency_key,
            reason="reached_checkout", charged=False,
        )


def test_stopped_before_payment_is_not_placed_and_frees_slot(tmp_path):
    cfg = tmp_path / "a.yaml"
    cfg.write_text("budget:\n  daily_max_usd: 25\n  auto_approve_under_usd: 18\n", encoding="utf-8")
    slots = tmp_path / "slots"
    result = run(cfg, provider=_StopProvider(), claim_slot=True, slot_dir=slots)
    assert result.decision.status is DecisionStatus.AUTO
    assert result.order_result.status is OrderStatus.STOPPED_BEFORE_PAYMENT
    assert result.placed is False                    # carted, not paid -> not placed
    assert list(slots.glob("*.json")) == []          # slot freed; the day stays open
    retry = run(cfg, provider=_StopProvider(), claim_slot=True, slot_dir=slots)
    assert retry.already_ran is False


def test_slot_claim_skips_done_and_recent_but_reclaims_stale(tmp_path):
    slots = tmp_path / "slots"
    slots.mkdir(mode=0o700)
    key = "daily-food-ordering-test"
    path = slots / f"{key}.json"

    path.write_text(json.dumps({"key": key, "state": "done"}), encoding="utf-8")
    assert _try_claim_slot(slots, key) is False          # an order was placed today

    path.write_text(json.dumps({"key": key, "state": "claimed",
                                "claimed_at": datetime.now(timezone.utc).isoformat()}), encoding="utf-8")
    assert _try_claim_slot(slots, key) is False          # a run is genuinely in progress

    stale = (datetime.now(timezone.utc) - timedelta(seconds=_CLAIM_STALE_SECONDS + 60)).isoformat()
    path.write_text(json.dumps({"key": key, "state": "claimed", "claimed_at": stale}), encoding="utf-8")
    assert _try_claim_slot(slots, key) is True           # crashed run -> reclaimed
