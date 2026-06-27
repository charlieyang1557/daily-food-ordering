# 1. BLOCK
#    - violates a hard restriction (re-check; should be impossible post-filter)
#         → Decision("BLOCK", "hard_violation", "P0")
#    - price > daily_max
#         → Decision("BLOCK", "over_daily_max", "P1")

# 2. CONFIRM   (price ≤ daily_max here)
#    - price > auto_approve_under                    → ("CONFIRM", "cost_band",   "P1")
#    - rolling_cap set and window_spend+price > cap  → ("CONFIRM", "rolling_cap", "P1")

# 3. AUTO
#    - else (≤ auto_approve, within rolling-cap, verified-safe)
#         → ("AUTO", "within_authority", None)

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from engine.config import load_config_from_dict
from engine.models import (
    Candidate,
    DecisionResult,
    DecisionStatus,
    Severity,
    UserConfig,
)


def decide(
    candidate: Candidate | Mapping[str, Any] | None,
    config: UserConfig | Mapping[str, Any],
    *,
    rolling_total_usd: float = 0,
    fallback_in_use: bool = False,
) -> DecisionResult:
    resolved_config = (
        load_config_from_dict(config) if isinstance(config, Mapping) else config
    )
    resolved_candidate = _candidate(candidate)

    if resolved_candidate is None:
        return _block("no_candidate", Severity.P0, None)
    if not resolved_candidate.available:
        return _block("unavailable", Severity.P2, resolved_candidate)

    safety_reason = _hard_restriction_reason(resolved_candidate, resolved_config)
    if safety_reason:
        return _block(safety_reason, Severity.P0, resolved_candidate)

    if resolved_candidate.price_usd > resolved_config.budget.daily_max_usd:
        return _block("over_daily_max", Severity.P1, resolved_candidate)

    rolling_cap = resolved_config.budget.rolling_cap_usd
    if (
        rolling_cap is not None
        and rolling_total_usd + resolved_candidate.price_usd > rolling_cap
    ):
        return DecisionResult(
            DecisionStatus.CONFIRM,
            "rolling_cap_exceeded",
            Severity.P1,
            resolved_candidate,
        )

    if fallback_in_use:
        return DecisionResult(
            DecisionStatus.CONFIRM,
            "fallback_in_use",
            Severity.P1,
            resolved_candidate,
        )

    if resolved_candidate.price_usd <= resolved_config.budget.auto_approve_under_usd:
        return DecisionResult(
            DecisionStatus.AUTO,
            "within_auto_approve",
            Severity.P2,
            resolved_candidate,
        )

    return DecisionResult(
        DecisionStatus.CONFIRM,
        "above_auto_approve",
        Severity.P1,
        resolved_candidate,
    )


def _candidate(candidate: Candidate | Mapping[str, Any] | None) -> Candidate | None:
    if candidate is None:
        return None
    if isinstance(candidate, Candidate):
        return candidate
    return Candidate.from_mapping(candidate)


def _hard_restriction_reason(candidate: Candidate, config: UserConfig) -> str | None:
    # A CONCRETE declared-allergen match is the most specific, most serious
    # violation — check it BEFORE the "can't verify" catch-all so a provider that
    # DOES declare an allergen (e.g. parsed from a menu description) yields a
    # precise allergy_violation rather than a generic unverified_safety. Both
    # BLOCK at P0; this only sharpens the reason. A POSITIVE allergen declaration
    # is trusted in the SAFE direction (to refuse) — we still never trust a
    # "this is safe" claim, so the verified_safe gate below is unchanged.
    allergies = set(_lowered(candidate.allergens))
    for allergy in _lowered(config.restrictions.allergies):
        if allergy in allergies:
            return "allergy_violation"

    needs_verification = bool(
        config.restrictions.allergies or config.restrictions.dietary
    )
    if needs_verification and not candidate.verified_safe:
        return "unverified_safety"

    candidate_dietary = set(_lowered(candidate.dietary))
    for restriction in _lowered(config.restrictions.dietary):
        # Dietary compliance requires a positive provider tag for each restriction.
        if restriction not in candidate_dietary:
            return "dietary_violation"

    restaurant = candidate.restaurant.strip().lower()
    cuisine = (candidate.cuisine or "").strip().lower()
    for forbidden in _lowered(config.restrictions.never_order):
        if forbidden in {restaurant, cuisine}:
            return "never_order"

    return None


def _lowered(values: Iterable[str]) -> list[str]:
    return [value.strip().lower() for value in values]


def _block(
    reason: str,
    severity: Severity,
    candidate: Candidate | None,
) -> DecisionResult:
    return DecisionResult(DecisionStatus.BLOCK, reason, severity, candidate)


decide_order = decide
resolve_decision = decide

__all__ = ["decide", "decide_order", "resolve_decision"]
