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
    name = "capture"

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

def test_degradation_reported_when_not_from_preferred(tmp_path):
    import yaml
    prov = _CaptureProvider(price=12)  # discovers/carts restaurant "R" (AUTO)
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({
        "budget": {"daily_max_usd": 50, "auto_approve_under_usd": 18},
        "preferences": {"favorite_restaurants": ["Thaibodia Bistro"]},
    }))
    s = run(str(p), provider=prov).order_result.summary
    assert s["degraded_from_preferred"] == "Thaibodia Bistro"
    assert s["ordered_from"] == "Pho Newark"
    assert "closed or unavailable" in s["degradation_reason"]
