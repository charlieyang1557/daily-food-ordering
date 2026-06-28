"""Tests for the post-review hardening fixes (code-review + Codex adversarial):
word-boundary keyword matching, full-name cart verification, --dish fail-closed,
and enforcing the auto-approve ceiling only for AUTO (not user-confirmed) orders.
"""
from engine.config import load_config_from_dict
from engine.models import Candidate
from providers.base import OrderResult, OrderStatus
from providers.doordash import DoorDashProvider as D
from run import _select_candidate, run


# ---- Fix 5: whole-word keyword matching (no substring false positives) --------

def test_drink_filter_matches_drinks_not_dishes():
    assert D._looks_like_drink("Coke")
    assert D._looks_like_drink("Thai Iced Tea")
    assert not D._looks_like_drink("Pad Thai")
    assert not D._looks_like_drink("Grilled Chicken")


def test_dessert_filter_no_substring_false_positive():
    assert D._looks_like_dessert("Mango Sticky Rice")
    assert not D._looks_like_dessert("Flank Steak")        # 'flan' must NOT match
    assert not D._looks_like_dessert("A3 - Sweet Chili")   # 'sweet' is not a keyword
    assert not D._looks_like_dessert("Grilled Pork")


def test_allergen_parse_word_boundary():
    assert D._parse_allergens("Pad Thai with peanut sauce") == ["peanuts"]
    assert D._parse_allergens("Crab Fried Rice") == ["shellfish"]
    assert D._parse_allergens("Crabapple-Glazed Pork") == []   # 'crab' NOT in 'crabapple'
    assert D._parse_allergens("Scalloped Potatoes") == []      # 'scallop' NOT in 'scalloped'
    assert D._parse_allergens("Grilled Chicken") == []


# ---- Fix 1: cart verification needles are the FULL name, never single words ---

def test_verify_needles_use_full_name_not_single_words():
    needles = D._verify_needles("A3 - Sweet Chili")
    assert "Sweet Chili" in needles
    assert "Sweet" not in needles and "Chili" not in needles
    # 'Pad Thai' must not degrade to a bare 'Thai' that substring-hits 'Thaibodia'.
    assert D._verify_needles("Pad Thai") == ["Pad Thai"]
    assert all(len(n) >= 4 for n in D._verify_needles("Pad Thai"))


# ---- Fix 3: --dish fails closed when the named dish isn't discovered ----------

def test_preferred_dish_no_match_fails_closed():
    cfg = load_config_from_dict({"budget": {"daily_max_usd": 50}})
    cands = [Candidate("R", "Green Curry", 15, verified_safe=True),
             Candidate("R", "Fried Rice", 12, verified_safe=True)]
    assert _select_candidate(cands, cfg, preferred_dish="pad thai") is None
    sel = _select_candidate(cands, cfg, preferred_dish="green curry")
    assert sel is not None and sel.item_name == "Green Curry"


# ---- Fix 2: auto-approve ceiling enforced for AUTO, suppressed for CONFIRM ----

class _CaptureProvider:
    name = "doordash"  # degradation report is gated to the real (doordash) provider

    def __init__(self, price):
        self.price = price
        self.kwargs = {}

    def discover(self, config):
        return [Candidate("Pho Newark", "Dish", self.price, verified_safe=True)]

    def place_order(self, candidate, **kwargs):
        self.kwargs = kwargs
        return OrderResult(
            status=OrderStatus.STOPPED_BEFORE_PAYMENT, provider=self.name,
            restaurant="Pho Newark", item_name="Dish", price_usd=candidate.price_usd,
            idempotency_key=kwargs["idempotency_key"], charged=False,
        )


def _cfg(tmp_path, **budget):
    import yaml
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"budget": {"daily_max_usd": 50, **budget}}))
    return str(p)


def test_auto_placement_enforces_auto_ceiling(tmp_path):
    prov = _CaptureProvider(price=12)  # <= auto 18 -> AUTO
    run(_cfg(tmp_path, auto_approve_under_usd=18), provider=prov)
    assert prov.kwargs["auto_approve_ceiling_usd"] == 18


def test_confirmed_placement_suppresses_auto_ceiling(tmp_path):
    prov = _CaptureProvider(price=25)  # 18 < 25 <= 50 -> CONFIRM
    run(_cfg(tmp_path, auto_approve_under_usd=18), provider=prov, confirmed=True)
    assert prov.kwargs["auto_approve_ceiling_usd"] is None


# ---- Degradation report: carted restaurant != preferred (closed favorite) -----

def _write(tmp_path, **extra):
    import yaml
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"budget": {"daily_max_usd": 50, "auto_approve_under_usd": 18}, **extra}))
    return str(p)


def test_degradation_reported_when_not_from_preferred(tmp_path):
    prov = _CaptureProvider(price=12)  # carts "Pho Newark" (AUTO)
    s = run(_write(tmp_path, preferences={"favorite_restaurants": ["Thaibodia Bistro"]}),
            provider=prov).order_result.summary
    assert s["degraded_from_preferred"] == "Thaibodia Bistro"
    assert s["ordered_from"] == "Pho Newark"
    assert "not available for this order" in s["degradation_reason"]


def test_no_false_degradation_for_favorite_spelling_variant(tmp_path):
    prov = _CaptureProvider(price=12)  # carts "Pho Newark"
    s = run(_write(tmp_path, preferences={"favorite_restaurants": ["Pho Newark!"]}),
            provider=prov).order_result.summary
    assert "degradation_reason" not in s  # punctuation variant of the carted store


def test_no_false_degradation_when_carted_is_the_fallback(tmp_path):
    prov = _CaptureProvider(price=12)  # carts "Pho Newark"
    s = run(_write(tmp_path,
                   preferences={"favorite_restaurants": ["Thaibodia Bistro"]},
                   fallback={"restaurant": "Pho Newark"}),
            provider=prov).order_result.summary
    assert "degradation_reason" not in s  # the pre-vetted fallback is not a "degradation"


def test_generic_one_word_favorite_does_not_suppress_degradation(tmp_path):
    prov = _CaptureProvider(price=12)  # carts "Pho Newark"
    s = run(_write(tmp_path, preferences={"favorite_restaurants": ["Pho"]}),
            provider=prov).order_result.summary
    # a bare "Pho" must NOT token-swallow the genuinely-different "Pho Newark"
    assert s.get("degradation_reason")


# ---- Fix: default --config is the unrestricted happy-path, not restricted prefs -

def test_default_config_is_unrestricted_demo_happy_path():
    # Interactive "order my daily food" must cart-and-stop even when the OpenClaw
    # agent drops --config; the default must NOT be the restricted user_preferences.
    from run import _parse_args
    args = _parse_args(["--provider", "doordash", "--claim-slot"])
    assert args.config == "demo/charlie-unrestricted.yaml"


# ---- Fix: strict clear_cart boolean (a destructive flag fails SAFE) ------------

def test_clear_cart_strict_boolean():
    import pytest
    from engine.config import ConfigError, load_config_from_dict
    base = {"budget": {"daily_max_usd": 25}}
    assert load_config_from_dict({**base, "clear_cart": True}).clear_cart is True
    assert load_config_from_dict({**base, "clear_cart": "false"}).clear_cart is False
    assert load_config_from_dict({**base, "clear_cart": "0"}).clear_cart is False
    assert load_config_from_dict(base).clear_cart is False
    with pytest.raises(ConfigError):
        load_config_from_dict({**base, "clear_cart": "maybe"})
