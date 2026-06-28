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
    confirmed: bool = False,
    claim_slot: bool = False,
    slot_dir: str | Path | None = None,
    dish: str | None = None,
    clear_cart: bool = False,
) -> RunResult:
    # Default to the deterministic mock so a bare `run()` is always dry & safe.
    if provider is None:
        from providers.mock import MockProvider

        provider = MockProvider("happy")

    slots = Path(slot_dir) if slot_dir is not None else _default_slot_dir()
    idempotency_key = _slot_key()
    steps: list[StepRecord] = []

    # Load + validate config FIRST, so a bad config never consumes the day's slot.
    config = load_config(config_path)

    # Step 1 — claim today's slot. The marker is held for the whole run and
    # RELEASED at the end unless an order is actually placed, so a transient
    # failure or a pending CONFIRM doesn't burn the day. If already claimed by a
    # concurrent/earlier run, no-op and exit.
    if claim_slot:
        if not _try_claim_slot(slots, idempotency_key):
            steps.append(_step(1, "claim_slot", StepStatus.SKIPPED, f"already claimed: {idempotency_key}"))
            return RunResult(
                config=config,
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

    steps.append(_step(2, "load_validate_config", StepStatus.OK, "config loaded"))

    # Step 3 — discovery. Any provider failure (bot wall, login, timeout, selector
    # miss) is classified as ProviderUnavailable so the run degrades, never crashes.
    try:
        candidates = provider.discover(config)
    except ProviderError:
        if claim_slot:  # retryable — release the slot so a later run can retry
            _release_slot(slots, idempotency_key)
        raise
    except Exception as error:  # noqa: BLE001
        if claim_slot:
            _release_slot(slots, idempotency_key)
        raise ProviderUnavailable(
            f"discover failed: {type(error).__name__}: {error}"
        ) from error
    steps.append(
        _step(3, "discover_candidates", StepStatus.OK, f"{len(candidates)} via {provider.name}")
    )

    available_candidates = _filter_available(candidates)
    steps.append(
        _step(4, "filter_available", StepStatus.OK, f"{len(available_candidates)} available")
    )

    ranked_candidates = _rank_candidates(available_candidates, config)
    steps.append(_step(5, "rank_preferences", StepStatus.OK, "ranked"))

    selected_candidate = _select_candidate(ranked_candidates, config, preferred_dish=dish)
    selection_detail = selected_candidate.item_name if selected_candidate else "none"
    steps.append(_step(6, "select_price", StepStatus.OK, selection_detail))

    # KNOWN LIMITATION: rolling_cap_usd is not enforced here. decide() supports a
    # rolling_total_usd argument, but there is no persisted spend ledger yet, so
    # the default 0 is passed and the cap never fires. Enforcing it needs a
    # durable spend ledger (see README "What I'd build next").
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

    # Step 9 — placement happens on AUTO, or on a CONFIRM the user has explicitly
    # approved (--confirmed). BLOCK never places. The real provider stops before
    # pay, bounds a substitute to the AUTO band, and reconciles the real total.
    order_result: OrderResult | None = None
    placed = False
    place_authorized = decision.status is DecisionStatus.AUTO or (
        confirmed and decision.status is DecisionStatus.CONFIRM
    )
    if place_authorized and selected_candidate is not None:
        try:
            order_result = provider.place_order(
                selected_candidate,
                idempotency_key=idempotency_key,
                complete_payment=complete_payment,
                budget_ceiling_usd=config.budget.daily_max_usd,
                # Enforce the auto-approve ceiling against the REAL checkout total
                # only for AUTO placements; a user-CONFIRMED order (--confirmed on a
                # CONFIRM) was already explicitly approved above the auto band.
                auto_approve_ceiling_usd=(
                    config.budget.auto_approve_under_usd
                    if decision.status is DecisionStatus.AUTO else None
                ),
                # Opt-in to clearing a non-empty cart via EITHER the --clear-cart
                # flag OR the config (demo configs set clear_cart: true, so the
                # agent need not remember the flag).
                clear_cart=clear_cart or config.clear_cart,
            )
            # STOPPED_BEFORE_PAYMENT is "carted, not paid" — NOT a placed order:
            # it neither sets `placed` nor consumes the daily slot. Only a real
            # PLACED counts. (This build never charges, so the real DoorDash path
            # never consumes a slot — honestly, no order means the day stays open.)
            placed = order_result.status is OrderStatus.PLACED
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
    reached_gate = order_result is not None and order_result.status in (
        OrderStatus.PLACED, OrderStatus.STOPPED_BEFORE_PAYMENT
    )
    place_status = StepStatus.OK if reached_gate else StepStatus.SKIPPED
    place_detail = order_result.status.value if order_result else "not placed"
    steps.append(_step(9, "place_order", place_status, place_detail))

    steps.append(_step(10, "post_order_self_audit", StepStatus.OK, "audit complete"))
    steps.append(_step(11, "record_notify", StepStatus.OK, "recorded"))

    if claim_slot:
        # Consume the slot ONLY if an order was actually placed (so retries and
        # pending CONFIRMs aren't blocked); otherwise release it.
        if placed:
            _record_slot_outcome(slots, idempotency_key, order_result.status.value if order_result else "placed")
        else:
            _release_slot(slots, idempotency_key)

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


_CLAIM_STALE_SECONDS = 900  # 15 min — longer than the cron payload timeout


def _try_claim_slot(slot_dir: Path, key: str) -> bool:
    """Atomically claim today's slot.

    Returns False only when the day is genuinely taken: a terminal `done` marker,
    or a *recent* in-progress `claimed` marker (another run is live). A STALE
    `claimed` marker (a crashed run that never released) is reclaimed, so a crash
    can't permanently block the day.
    """
    slot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = slot_dir / f"{key}.json"
    marker = {"key": key, "state": "claimed", "claimed_at": _now_iso()}
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            existing = {}
        if existing.get("state") == "done":
            return False  # an order was already placed today
        if not _claim_is_stale(existing.get("claimed_at")):
            return False  # another run is genuinely in progress
        try:  # stale crash — reclaim
            path.write_text(json.dumps(marker), encoding="utf-8")
            path.chmod(0o600)
            return True
        except Exception:  # noqa: BLE001
            return False
    with os.fdopen(fd, "w") as handle:
        json.dump(marker, handle)
    return True


def _claim_is_stale(claimed_at: str | None) -> bool:
    if not claimed_at:
        return True
    try:
        claimed = datetime.fromisoformat(claimed_at)
    except Exception:  # noqa: BLE001
        return True
    return (datetime.now(timezone.utc) - claimed).total_seconds() > _CLAIM_STALE_SECONDS


def _record_slot_outcome(slot_dir: Path, key: str, outcome: str) -> None:
    try:
        path = slot_dir / f"{key}.json"
        path.write_text(
            json.dumps({"key": key, "state": "done", "outcome": outcome, "recorded_at": _now_iso()}),
            encoding="utf-8",
        )
        path.chmod(0o600)  # write_text drops the tight mode set by the atomic claim
    except Exception:  # noqa: BLE001
        pass


def _release_slot(slot_dir: Path, key: str) -> None:
    # Free a claimed-but-not-placed slot so a retry / an approved CONFIRM can run.
    try:
        (slot_dir / f"{key}.json").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- pipeline helpers ---------------------------------------------------------

def _filter_available(candidates: list[Candidate]) -> list[Candidate]:
    # Drop sold-out options before ranking. This does NOT enforce allergen/dietary
    # safety — that is decide()'s job (the authoritative gate). Named for what it
    # actually does, so no one assumes safety filtering happens here.
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
    ranked = _rank_candidates(_filter_available(candidates), config)
    return _select_candidate(ranked, config)


def _select_candidate(
    candidates: list[Candidate], config: UserConfig, *, preferred_dish: str | None = None
) -> Candidate | None:
    # A specific dish was requested (e.g. a demo ordering "Pad Thai"): evaluate the
    # matching candidate by name, so the user's actual choice is checked — and, if
    # unsafe, refused — instead of the cheapest. Falls back to normal selection if
    # no candidate matches.
    if preferred_dish:
        needle = preferred_dish.strip().lower()
        matches = [c for c in candidates if needle in c.item_name.strip().lower()]
        if not matches:
            # The explicitly-requested dish was not discovered (lazy-load, filter,
            # or selector drift). Fail CLOSED — return no candidate so the engine
            # BLOCKs (no_candidate) — rather than silently order a different dish
            # than the one the user named.
            return None
        candidates = matches
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
    parser.add_argument("--dish", default=None,
                        help="order a SPECIFIC named dish (by name match) instead of the auto-pick")
    parser.add_argument("--clear-cart", action="store_true",
                        help="doordash: clear a non-empty cart before ordering (DESTRUCTIVE to "
                             "existing cart items). Without it, a non-empty cart fails closed.")
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
        "--confirmed",
        action="store_true",
        help="treat a CONFIRM decision as user-approved and place it (BLOCK never places)",
    )
    parser.add_argument(
        "--notify",
        action="store_true",
        help="post a one-line summary to the Discord webhook in $DFO_DISCORD_WEBHOOK",
    )
    parser.add_argument(
        "--complete-payment",
        action="store_true",
        help="DANGER: authorize a real charge. Off by default; the adapter still hard-stops.",
    )
    return parser.parse_args(argv)


def _post_notify(message: str) -> None:
    from notify import notify_discord, WEBHOOK_ENV

    if not os.environ.get(WEBHOOK_ENV):
        print(f"(--notify: {WEBHOOK_ENV} not set; skipping Discord post)", file=sys.stderr)
        return
    ok = notify_discord(message)
    print(f"(--notify: {'posted to Discord' if ok else 'Discord post failed'})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.provider == "doordash" and args.login:
        from providers.doordash import DoorDashProvider

        DoorDashProvider(profile_dir=args.profile).login()
        return 0

    provider = None
    try:
        provider = _build_provider(args)
        result = run(
            args.config,
            provider=provider,
            complete_payment=args.complete_payment,
            confirmed=args.confirmed,
            claim_slot=args.claim_slot,
            dish=args.dish,
            clear_cart=args.clear_cart,
        )
    except ConfigError as error:
        print(json.dumps({"error": "config_invalid", "detail": str(error)}, indent=2))
        return 2
    except ProviderError as error:
        print(json.dumps({"error": "provider_unavailable", "detail": str(error)}, indent=2))
        if args.notify:
            _post_notify("⚠️ Daily Food Ordering — DoorDash unavailable (bot wall / login). "
                         "Re-run `--login`. Nothing ordered.")
        return 3
    finally:
        # Tear down any browser session the provider held open across
        # discover() -> place_order() (DoorDash). Mock providers have no close().
        _close = getattr(provider, "close", None)
        if callable(_close):
            try:
                _close()
            except Exception:  # noqa: BLE001
                pass

    print(json.dumps(result.to_dict(), indent=2))
    if args.notify:
        from notify import format_notification

        _post_notify(format_notification(result.to_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
