"""Tests for the review-driven hardening:
- durable atomic slot claim (Codex #2)
- run() crash-proofing for unexpected provider exceptions (CR #7/#8/#13)
- real checkout-total reconciliation vs budget, fail closed (Codex #3 / CR #14)
- cheapest-in-budget tie-break in selection (CR #5)
"""
import pytest

from engine.config import load_config_from_dict
from engine.models import Candidate, DecisionStatus
from providers.base import OrderStatus, ProviderUnavailable
from providers.doordash import DoorDashProvider
from providers.mock import MockProvider
from run import run, _rank_candidates, _select_candidate


def _cfg(tmp_path, daily=25, auto=18):
    path = tmp_path / "c.yaml"
    path.write_text(f"budget:\n  daily_max_usd: {daily}\n  auto_approve_under_usd: {auto}\n", encoding="utf-8")
    return path


# ---- durable slot claim ----------------------------------------------------

def test_claim_slot_runs_once_then_skips(tmp_path):
    cfg = _cfg(tmp_path)
    slots = tmp_path / "slots"
    r1 = run(cfg, provider=MockProvider("happy"), claim_slot=True, slot_dir=slots)
    assert r1.already_ran is False and r1.placed is True
    r2 = run(cfg, provider=MockProvider("happy"), claim_slot=True, slot_dir=slots)
    assert r2.already_ran is True and r2.placed is False
    assert (slots / f"{r1.idempotency_key}.json").exists()


def test_without_claim_slot_writes_no_marker(tmp_path):
    slots = tmp_path / "slots"
    run(_cfg(tmp_path), provider=MockProvider("happy"), slot_dir=slots)
    assert not slots.exists()


# ---- run() crash-proofing --------------------------------------------------

class _BoomDiscover:
    name = "boom"

    def discover(self, config):
        raise RuntimeError("selector kaboom")  # NOT a ProviderError

    def place_order(self, *a, **k):  # pragma: no cover
        raise AssertionError


class _BoomPlace:
    name = "boomplace"

    def discover(self, config):
        return [Candidate("X", "Y", 5, verified_safe=True)]

    def place_order(self, candidate, *, idempotency_key, complete_payment=False, budget_ceiling_usd=None):
        raise TimeoutError("nav timeout")  # NOT a ProviderError


def test_unexpected_discover_exception_becomes_provider_unavailable(tmp_path):
    with pytest.raises(ProviderUnavailable):
        run(_cfg(tmp_path), provider=_BoomDiscover())


def test_unexpected_place_order_exception_is_recorded_not_raised(tmp_path):
    result = run(_cfg(tmp_path), provider=_BoomPlace())
    assert result.decision.status is DecisionStatus.AUTO
    assert result.placed is False
    assert result.order_result.status is OrderStatus.FAILED
    assert "TimeoutError" in result.order_result.reason
    assert result.order_result.charged is False


# ---- checkout-total reconciliation (fail closed) ---------------------------

def _cand():
    return Candidate("Thai Spice", "Pad Thai", 14, verified_safe=True)


def test_reconcile_within_budget_proceeds():
    assert DoorDashProvider()._reconcile_budget(
        _cand(), idempotency_key="k", total=18.4, ceiling=25, summary={}, screenshot_path=None
    ) is None


def test_reconcile_over_budget_blocks_and_never_charges():
    res = DoorDashProvider()._reconcile_budget(
        _cand(), idempotency_key="k", total=40.0, ceiling=25, summary={}, screenshot_path=None
    )
    assert res.status is OrderStatus.BLOCKED
    assert res.charged is False
    assert res.summary["checkout_total_usd"] == 40.0


def test_reconcile_unverifiable_total_fails_closed():
    res = DoorDashProvider()._reconcile_budget(
        _cand(), idempotency_key="k", total=None, ceiling=25, summary={}, screenshot_path=None
    )
    assert res.status is OrderStatus.FAILED
    assert res.charged is False


def test_reconcile_no_ceiling_proceeds():
    assert DoorDashProvider()._reconcile_budget(
        _cand(), idempotency_key="k", total=None, ceiling=None, summary={}, screenshot_path=None
    ) is None


# ---- cheapest-in-budget tie-break ------------------------------------------

def test_equal_rank_selects_the_cheaper_option():
    config = load_config_from_dict({"budget": {"daily_max_usd": 25}})
    pricey = Candidate("R", "Pricey", 20, cuisine="Thai", verified_safe=True)
    cheap = Candidate("R", "Cheap", 10, cuisine="Thai", verified_safe=True)
    ranked = _rank_candidates([pricey, cheap], config)
    assert _select_candidate(ranked, config).item_name == "Cheap"
