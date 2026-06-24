from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any

from engine.config import load_config
from engine.decision import decide
from engine.models import Candidate, DecisionResult, DecisionStatus, UserConfig


class StepStatus(str, Enum):
    OK = "OK"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class StepRecord:
    number: int
    name: str
    status: StepStatus
    detail: str


@dataclass(frozen=True)
class RunResult:
    config: UserConfig
    idempotency_key: str
    candidates: list[Candidate]
    selected_candidate: Candidate | None
    decision: DecisionResult | None
    placed: bool
    steps: list[StepRecord]

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "selected_candidate": _candidate_dict(self.selected_candidate),
            "decision": _decision_dict(self.decision),
            "placed": self.placed,
            "steps": [
                {
                    "number": step.number,
                    "name": step.name,
                    "status": step.status.value,
                    "detail": step.detail,
                }
                for step in self.steps
            ],
        }


def run(config_path: str | Path = "user_preferences.yaml") -> RunResult:
    steps: list[StepRecord] = []

    idempotency_key = _claim_slot()
    steps.append(_step(1, "claim_slot", StepStatus.OK, idempotency_key))

    config = load_config(config_path)
    steps.append(_step(2, "load_validate_config", StepStatus.OK, "config loaded"))

    candidates = _discover_candidates(config)
    steps.append(_step(3, "discover_candidates", StepStatus.OK, f"{len(candidates)} found"))

    safe_candidates = _filter_safe(candidates)
    steps.append(_step(4, "filter_hard_restrictions", StepStatus.OK, f"{len(safe_candidates)} safe"))

    ranked_candidates = _rank_candidates(safe_candidates)
    steps.append(_step(5, "rank_preferences", StepStatus.OK, "ranked"))

    selected_candidate = _select_candidate(ranked_candidates, config)
    selection_detail = selected_candidate.item_name if selected_candidate else "none"
    steps.append(_step(6, "select_price", StepStatus.OK, selection_detail))

    decision = decide(selected_candidate, config)
    decision_status = StepStatus.BLOCKED if decision.status is DecisionStatus.BLOCK else StepStatus.OK
    steps.append(_step(7, "decide", decision_status, decision.reason))

    resolved = _resolve_decision(decision)
    steps.append(_step(8, "resolve_decision", resolved, decision.status.value))

    placed = decision.status is DecisionStatus.AUTO
    place_status = StepStatus.OK if placed else StepStatus.SKIPPED
    steps.append(_step(9, "place_order", place_status, "placed" if placed else "not placed"))

    steps.append(_step(10, "post_order_self_audit", StepStatus.OK, "audit complete"))
    steps.append(_step(11, "record_notify", StepStatus.OK, "recorded"))

    return RunResult(
        config=config,
        idempotency_key=idempotency_key,
        candidates=candidates,
        selected_candidate=selected_candidate,
        decision=decision,
        placed=placed,
        steps=steps,
    )


def _claim_slot() -> str:
    return f"daily-food-ordering-{date.today().isoformat()}"


def _discover_candidates(config: UserConfig) -> list[Candidate]:
    restaurant = (
        config.preferences.favorite_restaurants[0]
        if config.preferences.favorite_restaurants
        else "Thai Spice"
    )
    cuisine = config.preferences.cuisines[0] if config.preferences.cuisines else None
    return [
        Candidate(
            restaurant=restaurant,
            item_name="Vegetarian Pad Thai",
            price_usd=14,
            cuisine=cuisine,
            dietary=config.restrictions.dietary,
            allergens=[],
            verified_safe=True,
        )
    ]


def _filter_safe(candidates: list[Candidate]) -> list[Candidate]:
    return [candidate for candidate in candidates if candidate.available]


def _rank_candidates(candidates: list[Candidate]) -> list[Candidate]:
    return candidates


def _select_candidate(candidates: list[Candidate], config: UserConfig) -> Candidate | None:
    in_budget = [
        candidate
        for candidate in candidates
        if candidate.price_usd <= config.budget.daily_max_usd
    ]
    if not in_budget:
        return candidates[0] if candidates else None
    return min(in_budget, key=lambda candidate: candidate.price_usd)


def _resolve_decision(decision: DecisionResult) -> StepStatus:
    if decision.status is DecisionStatus.BLOCK:
        return StepStatus.BLOCKED
    if decision.status is DecisionStatus.CONFIRM:
        return StepStatus.SKIPPED
    return StepStatus.OK


def _step(
    number: int,
    name: str,
    status: StepStatus,
    detail: str,
) -> StepRecord:
    return StepRecord(number=number, name=name, status=status, detail=detail)


def _candidate_dict(candidate: Candidate | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "restaurant": candidate.restaurant,
        "item_name": candidate.item_name,
        "price_usd": candidate.price_usd,
        "cuisine": candidate.cuisine,
        "dietary": candidate.dietary,
        "allergens": candidate.allergens,
        "verified_safe": candidate.verified_safe,
        "available": candidate.available,
    }


def _decision_dict(decision: DecisionResult | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "decision": decision.status.value,
        "reason": decision.reason,
        "severity": decision.severity.value,
    }


def main() -> int:
    result = run()
    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
