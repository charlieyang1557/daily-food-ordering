from engine.config import load_config_from_dict
from engine.decision import decide
from engine.models import (
    BudgetConfig,
    Candidate,
    DecisionStatus,
    RestrictionsConfig,
    Severity,
    UserConfig,
)


def test_decide_auto_when_safe_and_under_auto_approve_threshold():
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18}}
    )
    candidate = Candidate(
        restaurant="Thai Spice",
        item_name="Vegetarian Pad Thai",
        price_usd=14,
        dietary=["vegetarian"],
        allergens=[],
    )

    result = decide(candidate, config)

    assert result.status is DecisionStatus.AUTO
    assert result.reason == "within_auto_approve"


def test_decide_confirm_when_safe_but_above_auto_approve_threshold():
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18}}
    )
    candidate = Candidate(
        restaurant="Sushi Place",
        item_name="Veggie Bento",
        price_usd=21,
        dietary=["vegetarian"],
        allergens=[],
    )

    result = decide(candidate, config)

    assert result.status is DecisionStatus.CONFIRM
    assert result.reason == "above_auto_approve"


def test_decide_blocks_when_price_exceeds_daily_max():
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18}}
    )
    candidate = Candidate("Thai Spice", "Curry", 30)

    result = decide(candidate, config)

    assert result.status is DecisionStatus.BLOCK
    assert result.reason == "over_daily_max"
    assert result.severity is Severity.P1


def test_allergen_blocks_even_when_candidate_is_under_auto_approve_threshold():
    config = load_config_from_dict(
        {
            "budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18},
            "restrictions": {"allergies": ["peanuts"]},
        }
    )
    candidate = Candidate(
        "Thai Spice",
        "Peanut Noodles",
        14,
        allergens=["peanuts"],
        verified_safe=True,
    )

    result = decide(candidate, config)

    assert result.status is DecisionStatus.BLOCK
    assert result.reason == "allergy_violation"
    assert result.severity is Severity.P0


def test_restricted_candidate_defaults_to_unverified_until_provider_confirms():
    config = load_config_from_dict(
        {
            "budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18},
            "restrictions": {"allergies": ["peanuts"]},
        }
    )
    candidate = Candidate("Thai Spice", "Pad Thai", 14)

    result = decide(candidate, config)

    assert result.status is DecisionStatus.BLOCK
    assert result.reason == "unverified_safety"
    assert result.severity is Severity.P0


def test_unrestricted_user_can_auto_unverified_candidate():
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25, "auto_approve_under_usd": 18}}
    )
    candidate = Candidate(
        "Corner Deli",
        "Turkey Sandwich",
        12,
        verified_safe=False,
    )

    result = decide(candidate, config)

    assert result.status is DecisionStatus.AUTO
    assert result.reason == "within_auto_approve"


def test_decide_blocks_hard_line_crossings_case_insensitively():
    config = UserConfig(
        budget=BudgetConfig(daily_max_usd=25, auto_approve_under_usd=18),
        restrictions=RestrictionsConfig(
            dietary=["Vegetarian"],
            allergies=["Peanuts"],
            never_order=["KFC"],
        ),
    )

    over_budget = Candidate(
        "Thai Spice",
        "Curry",
        30,
        dietary=["vegetarian"],
        verified_safe=True,
    )
    allergen_risk = Candidate(
        "Thai Spice",
        "Peanut Noodles",
        14,
        dietary=["vegetarian"],
        allergens=["peanuts"],
        verified_safe=True,
    )
    never_order = Candidate(
        "KFC",
        "Side Salad",
        9,
        dietary=["vegetarian"],
        verified_safe=True,
    )
    unsafe_and_over_budget = Candidate(
        "Thai Spice",
        "Peanut Curry",
        30,
        dietary=["vegetarian"],
        allergens=["peanuts"],
        verified_safe=True,
    )

    assert decide(over_budget, config).reason == "over_daily_max"
    assert decide(allergen_risk, config).reason == "allergy_violation"
    assert decide(never_order, config).decision is DecisionStatus.BLOCK

    safety_first = decide(unsafe_and_over_budget, config)
    assert safety_first.reason == "allergy_violation"
    assert safety_first.severity is Severity.P0
