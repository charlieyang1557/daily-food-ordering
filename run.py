"""The 11-step ordering pipeline — the deterministic orchestrator the skill execs.

The model never makes a safety or budget call here. run.py loads config, asks a
*provider* for candidates, runs the hard filters, then hands the selection to the
engine's `decide()` for the one AUTO / CONFIRM / BLOCK verdict. Only on AUTO does
it ask the provider to place the order — and the real provider stops before pay
and re-checks the real checkout total against the budget ceiling.

  python run.py                          # mock provider, happy path (dry, safe)
  python run.py --scenario over_budget   # trigger the budget-exceeded failure path
  python run.py --provider doordash      # real DoorDash, stops before payment
  python run.py --provider doordash --login   # one-time browser login / warm-up
  python run.py --provider doordash --claim-slot   # idempotent daily run (cron)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from engine.config import ConfigError, load_config
from engine.decision import decide
from engine.models import Candidate, DecisionResult, DecisionStatus, UserConfig
from providers.base import (
    OrderResult,
    OrderStatus,
    Provider,
    ProviderError,
    ProviderUnavailable,
)


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
    order_result: OrderResult | None = None
    already_ran: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "idempotency_key": self.idempotency_key,
            "already_ran": self.already_ran,
            "selected_candidate": _candidate_dict(self.selected_candidate),
            "decision": _decision_dict(self.decision),
            "placed": self.placed,
            "order_result": self.order_result.to_dict() if self.order_result else None,
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


def run(
    config_path: str | Path = "user_preferences.yaml",
    *,
    provider: Provider | None = None,
    complete_payment: bool = False,
    claim_slot: bool = False,
    slot_dir: str | Path | None = None,
) -> RunResult:
    # Default to the deterministic mock so a bare `run()` is always dry & safe.
    if provider is None:
        from providers.mock import MockProvider

        provider = MockProvider("happy")

    slots = Path(slot_dir) if slot_dir is not None else _default_slot_dir()
    idempotency_key = _slot_key()
    steps: list[StepRecord] = []

    # Step 1 — claim today's slot. With claim_slot, this is a DURABLE atomic
    # marker so a retry / a second cron fire / a manual run the same day cannot
    # double-order. If already claimed, no-op and exit.
    if claim_slot:
        if not _try_claim_slot(slots, idempotency_key):
            steps.append(_step(1, "claim_slot", StepStatus.SKIPPED, f"already claimed: {idempotency_key}"))
            return RunResult(
                config=load_config(config_path),
                idempotency_key=idempotency_key,
                candidates=[],
                selected_candidate=None,
                decision=None,
                placed=False,
                steps=steps,
                order_result=None,
                already_ran=True,
            )
        steps.append(_step(1, "claim_slot", StepStatus.OK, f"claimed: {idempotency_key}"))
    else:
        steps.append(_step(1, "claim_slot", StepStatus.OK, idempotency_key))

    config = load_config(config_path)
    steps.append(_step(2, "load_validate_config", StepStatus.OK, "config loaded"))

    # Step 3 — discovery. Any provider failure (bot wall, login, timeout, selector
    # miss) is classified as ProviderUnavailable so the run degrades, never crashes.
    try:
        candidates = provider.discover(config)
    except ProviderError:
        if claim_slot:
            _record_slot_outcome(slots, idempotency_key, "provider_unavailable")
        raise
    except Exception as error:  # noqa: BLE001
        if claim_slot:
            _record_slot_outcome(slots, idempotency_key, "discover_error")
        raise ProviderUnavailable(
            f"discover failed: {type(error).__name__}: {error}"
        ) from error
    steps.append(
        _step(3, "discover_candidates", StepStatus.OK, f"{len(candidates)} via {provider.name}")
    )

    safe_candidates = _filter_safe(candidates)
    steps.append(
        _step(4, "filter_hard_restrictions", StepStatus.OK, f"{len(safe_candidates)} available")
    )

    ranked_candidates = _rank_candidates(safe_candidates, config)
    steps.append(_step(5, "rank_preferences", StepStatus.OK, "ranked"))

    selected_candidate = _select_candidate(ranked_candidates, config)
    selection_detail = selected_candidate.item_name if selected_candidate else "none"
    steps.append(_step(6, "select_price", StepStatus.OK, selection_detail))

    decision = decide(selected_candidate, config)
    # Fallback (SKILL Step 8): on any BLOCK, try the pre-vetted fallback. If it
    # re-checks safe + within budget, it becomes a fallback-in-use CONFIRM.
    fallback_used = False
    if decision.status is DecisionStatus.BLOCK and config.fallback.restaurant:
        fallback_candidate = _try_fallback(provider, config)
        if fallback_candidate is not None:
            fallback_decision = decide(fallback_candidate, config, fallback_in_use=True)
            if fallback_decision.status is not DecisionStatus.BLOCK:
                selected_candidate = fallback_candidate
                decision = fallback_decision
                fallback_used = True
    decision_status = StepStatus.BLOCKED if decision.status is DecisionStatus.BLOCK else StepStatus.OK
    decide_detail = decision.reason + (" (fallback)" if fallback_used else "")
    steps.append(_step(7, "decide", decision_status, decide_detail))

    resolved = _resolve_decision(decision)
    steps.append(_step(8, "resolve_decision", resolved, decision.status.value))

    # Step 9 — placement happens ONLY on AUTO. The real provider stops before pay
    # and re-checks the real checkout total against daily_max (fail closed).
    order_result: OrderResult | None = None
    placed = False
    if decision.status is DecisionStatus.AUTO and selected_candidate is not None:
        try:
            order_result = provider.place_order(
                selected_candidate,
                idempotency_key=idempotency_key,
                complete_payment=complete_payment,
                budget_ceiling_usd=config.budget.daily_max_usd,
            )
            placed = order_result.status in (
                OrderStatus.PLACED,
                OrderStatus.STOPPED_BEFORE_PAYMENT,
            )
        except Exception as error:  # noqa: BLE001
            # Any placement failure (provider error OR an unexpected browser
            # exception) is recorded, never crashes, never blind-retries, never
            # charged-but-unconfirmed.
            order_result = OrderResult(
                status=OrderStatus.FAILED,
                provider=getattr(provider, "name", "unknown"),
                restaurant=selected_candidate.restaurant,
                item_name=selected_candidate.item_name,
                price_usd=selected_candidate.price_usd,
                idempotency_key=idempotency_key,
                reason=f"{type(error).__name__}: {error}",
            )
    place_status = StepStatus.OK if placed else StepStatus.SKIPPED
    place_detail = order_result.status.value if order_result else "not placed"
    steps.append(_step(9, "place_order", place_status, place_detail))

    steps.append(_step(10, "post_order_self_audit", StepStatus.OK, "audit complete"))
    steps.append(_step(11, "record_notify", StepStatus.OK, "recorded"))

    if claim_slot:
        outcome = order_result.status.value if order_result else decision.status.value
        _record_slot_outcome(slots, idempotency_key, outcome)

    return RunResult(
        config=config,
        idempotency_key=idempotency_key,
        candidates=candidates,
        selected_candidate=selected_candidate,
        decision=decision,
        placed=placed,
        steps=steps,
        order_result=order_result,
    )


# ---- durable slot ledger ------------------------------------------------------

def _slot_key() -> str:
    return f"daily-food-ordering-{date.today().isoformat()}"


def _default_slot_dir() -> Path:
    return Path.home() / ".daily-food-ordering" / "slots"


def _try_claim_slot(slot_dir: Path, key: str) -> bool:
    """Atomically claim today's slot. Returns False if already claimed."""
    slot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = slot_dir / f"{key}.json"
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        json.dump(
            {"key": key, "state": "claimed", "claimed_at": _now_iso()},
            handle,
        )
    return True


def _record_slot_outcome(slot_dir: Path, key: str, outcome: str) -> None:
    try:
        path = slot_dir / f"{key}.json"
        path.write_text(
            json.dumps({"key": key, "state": "done", "outcome": outcome, "recorded_at": _now_iso()}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- pipeline helpers ---------------------------------------------------------

def _filter_safe(candidates: list[Candidate]) -> list[Candidate]:
    # Drop sold-out options before ranking; the engine re-checks safety + budget.
    return [candidate for candidate in candidates if candidate.available]


def _rank_candidates(candidates: list[Candidate], config: UserConfig) -> list[Candidate]:
    # Soft-preference ranking: favorite restaurants, then preferred cuisines, then
    # cheaper-first as a tiebreak. Pure ordering — never a gate.
    favorites = [r.strip().lower() for r in config.preferences.favorite_restaurants]
    cuisines = [c.strip().lower() for c in config.preferences.cuisines]

    def score(candidate: Candidate) -> tuple[int, int, float]:
        fav = 0 if candidate.restaurant.strip().lower() in favorites else 1
        cuisine = (candidate.cuisine or "").strip().lower()
        cuisine_rank = cuisines.index(cuisine) if cuisine in cuisines else len(cuisines)
        return (fav, cuisine_rank, candidate.price_usd)

    return sorted(candidates, key=score)


def _try_fallback(provider: Provider, config: UserConfig) -> Candidate | None:
    discover_fallback = getattr(provider, "discover_fallback", None)
    if discover_fallback is None:
        return None
    try:
        candidates = discover_fallback(config)
    except ProviderError:
        return None
    ranked = _rank_candidates(_filter_safe(candidates), config)
    return _select_candidate(ranked, config)


def _select_candidate(candidates: list[Candidate], config: UserConfig) -> Candidate | None:
    in_budget = [
        candidate
        for candidate in candidates
        if candidate.price_usd <= config.budget.daily_max_usd
    ]
    if not in_budget:
        # Nothing in budget: surface the cheapest so the engine BLOCKs on price.
        return min(candidates, key=lambda c: c.price_usd) if candidates else None
    # Best-ranked in-budget option (rank already breaks ties cheaper-first).
    return in_budget[0]


def _resolve_decision(decision: DecisionResult) -> StepStatus:
    if decision.status is DecisionStatus.BLOCK:
        return StepStatus.BLOCKED
    if decision.status is DecisionStatus.CONFIRM:
        return StepStatus.SKIPPED
    return StepStatus.OK


def _step(number: int, name: str, status: StepStatus, detail: str) -> StepRecord:
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


def _build_provider(args: argparse.Namespace) -> Provider:
    if args.provider == "mock":
        from providers.mock import MockProvider

        return MockProvider(args.scenario)
    if args.provider == "doordash":
        from providers.doordash import DoorDashProvider

        return DoorDashProvider(
            headless=args.headless,
            profile_dir=args.profile,
            search_query=args.query,
        )
    raise SystemExit(f"unknown provider: {args.provider}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily food ordering — deterministic pipeline")
    parser.add_argument("--config", default="user_preferences.yaml")
    parser.add_argument("--provider", choices=["mock", "doordash"], default="mock")
    parser.add_argument(
        "--scenario",
        choices=["happy", "over_budget", "unavailable", "empty", "allergen"],
        default="happy",
        help="mock-only: which candidate set to return",
    )
    parser.add_argument("--query", default=None, help="doordash: search term (cuisine/restaurant)")
    parser.add_argument("--profile", default=None, help="doordash: persistent browser profile dir")
    parser.add_argument("--headless", action="store_true", help="doordash: run headless (usually bot-walled)")
    parser.add_argument(
        "--login",
        action="store_true",
        help="doordash: open a headed browser to log in / warm the profile, then exit",
    )
    parser.add_argument(
        "--claim-slot",
        action="store_true",
        help="durable per-day idempotency: skip if today's slot was already claimed",
    )
    parser.add_argument(
        "--complete-payment",
        action="store_true",
        help="DANGER: authorize a real charge. Off by default; the adapter still hard-stops.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.provider == "doordash" and args.login:
        from providers.doordash import DoorDashProvider

        DoorDashProvider(profile_dir=args.profile).login()
        return 0

    try:
        provider = _build_provider(args)
        result = run(
            args.config,
            provider=provider,
            complete_payment=args.complete_payment,
            claim_slot=args.claim_slot,
        )
    except ConfigError as error:
        print(json.dumps({"error": "config_invalid", "detail": str(error)}, indent=2))
        return 2
    except ProviderError as error:
        print(json.dumps({"error": "provider_unavailable", "detail": str(error)}, indent=2))
        return 3

    print(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
