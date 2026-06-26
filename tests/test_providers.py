"""Tests for the provider boundary: the mock provider + OrderResult contract.

The mock provider is the deterministic stand-in the engine tests run against,
and the lever that makes failure paths (over-budget, unavailable, empty,
allergen) triggerable on demand.
"""
from engine.config import load_config_from_dict
from engine.decision import decide
from engine.models import Candidate, DecisionStatus
from providers.base import OrderResult, OrderStatus, Provider
from providers.mock import MockProvider


def _config(**budget):
    base = {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18}}
    base["budget"].update(budget)
    return load_config_from_dict(base)


def test_mock_provider_satisfies_provider_protocol():
    provider = MockProvider()
    assert isinstance(provider, Provider)
    assert provider.name == "mock"


def test_happy_scenario_returns_the_v1_stub_candidate():
    # The walking-skeleton candidate the run-pipeline test pins to.
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18},
         "restrictions": {"dietary": ["vegetarian"]}}
    )
    candidates = MockProvider("happy").discover(config)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.restaurant == "Thai Spice"
    assert c.item_name == "Vegetarian Pad Thai"
    assert c.price_usd == 14
    assert c.verified_safe is True
    # carries the user's dietary tags so the safety gate clears for a veg user
    assert "vegetarian" in [d.lower() for d in c.dietary]


def test_over_budget_scenario_drives_engine_to_block():
    config = _config()
    candidate = MockProvider("over_budget").discover(config)[0]
    result = decide(candidate, config)
    assert result.status is DecisionStatus.BLOCK
    assert result.reason == "over_daily_max"


def test_unavailable_scenario_drives_engine_to_block():
    config = _config()
    candidate = MockProvider("unavailable").discover(config)[0]
    assert candidate.available is False
    result = decide(candidate, config)
    assert result.status is DecisionStatus.BLOCK
    assert result.reason == "unavailable"


def test_empty_scenario_returns_no_candidates():
    assert MockProvider("empty").discover(_config()) == []


def test_unknown_scenario_is_rejected_loudly():
    import pytest

    with pytest.raises(ValueError):
        MockProvider("teleport")


def test_place_order_returns_structured_placed_result_and_never_charges():
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18}}
    )
    candidate = MockProvider("happy").discover(config)[0]
    result = MockProvider("happy").place_order(candidate, idempotency_key="k-123")
    assert isinstance(result, OrderResult)
    assert result.status is OrderStatus.PLACED
    assert result.provider == "mock"
    assert result.restaurant == "Thai Spice"
    assert result.idempotency_key == "k-123"
    assert result.charged is False  # the mock simulates; it never moves money
    assert result.to_dict()["status"] == "PLACED"
