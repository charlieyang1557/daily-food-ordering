"""The fallback flow (SKILL Step 8): on a BLOCK, a configured safe fallback
becomes a fallback-in-use CONFIRM; with no fallback the BLOCK stands.
"""
from engine.models import DecisionStatus
from providers.mock import MockProvider
from run import run


def _cfg(tmp_path, *, fallback: bool):
    lines = [
        "budget:", "  daily_max_usd: 25", "  auto_approve_under_usd: 18",
        "restrictions:", "  allergies: [peanuts]",
    ]
    if fallback:
        lines += ["fallback:", "  restaurant: Chipotle"]
    path = tmp_path / "c.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_allergy_block_settles_on_safe_fallback(tmp_path):
    # mock 'allergen' returns a peanut dish -> BLOCK; the Chipotle fallback is
    # safe + in budget -> fallback-in-use CONFIRM.
    result = run(_cfg(tmp_path, fallback=True), provider=MockProvider("allergen"))
    assert result.decision.status is DecisionStatus.CONFIRM
    assert result.decision.reason == "fallback_in_use"
    assert result.selected_candidate.restaurant == "Chipotle"
    assert result.placed is False  # a CONFIRM is not auto-placed by run.py


def test_allergy_block_without_fallback_stays_blocked(tmp_path):
    result = run(_cfg(tmp_path, fallback=False), provider=MockProvider("allergen"))
    assert result.decision.status is DecisionStatus.BLOCK
    assert result.decision.reason == "allergy_violation"
    assert result.placed is False
