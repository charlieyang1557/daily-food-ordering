"""Coverage for engine guardrail branches the original suite didn't pin:
the two CONFIRM triggers (rolling cap, fallback-in-use) and the remaining
hard-restriction reasons (never_order, dietary_violation). engine/ is unchanged;
these tests assert its actual behavior.
"""
from engine.config import load_config_from_dict
from engine.decision import decide
from engine.models import Candidate, DecisionStatus, Severity


def _safe_candidate(price=14):
    return Candidate("Thai Spice", "Veg Pad Thai", price, dietary=["vegetarian"], verified_safe=True)


def _config(**budget):
    base = {"daily_max_usd": 25, "auto_approve_under_usd": 18}
    base.update(budget)
    return load_config_from_dict({"budget": base, "restrictions": {"dietary": ["vegetarian"]}})


def test_rolling_cap_exceeded_confirms():
    config = _config(rolling_cap_usd=150)
    # 140 already spent this window; +14 = 154 > 150 cap.
    result = decide(_safe_candidate(14), config, rolling_total_usd=140)
    assert result.status is DecisionStatus.CONFIRM
    assert result.reason == "rolling_cap_exceeded"
    assert result.severity is Severity.P1


def test_rolling_cap_exactly_at_cap_does_not_confirm():
    config = _config(rolling_cap_usd=150)
    # 136 + 14 = 150, not over the cap -> stays AUTO (price is under auto-approve).
    result = decide(_safe_candidate(14), config, rolling_total_usd=136)
    assert result.status is DecisionStatus.AUTO


def test_fallback_in_use_confirms():
    config = _config()
    result = decide(_safe_candidate(14), config, fallback_in_use=True)
    assert result.status is DecisionStatus.CONFIRM
    assert result.reason == "fallback_in_use"
    assert result.severity is Severity.P1


def test_never_order_blocks_with_reason():
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18},
         "restrictions": {"never_order": ["KFC"]}}
    )
    candidate = Candidate("KFC", "Side Salad", 9, verified_safe=True)
    result = decide(candidate, config)
    assert result.status is DecisionStatus.BLOCK
    assert result.reason == "never_order"


def test_dietary_violation_blocks_with_reason():
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18},
         "restrictions": {"dietary": ["vegan"]}}
    )
    # Provider-confirmed safe, but the positive vegan tag is absent.
    candidate = Candidate("Bowl Co", "Chicken Bowl", 12, dietary=["gluten-free"], verified_safe=True)
    result = decide(candidate, config)
    assert result.status is DecisionStatus.BLOCK
    assert result.reason == "dietary_violation"
    assert result.severity is Severity.P0
