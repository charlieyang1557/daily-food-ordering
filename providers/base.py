"""The provider boundary.

v1 left this as a "planned" seam (discovery was an inline stub in run.py). v2
formalizes it so the deterministic engine never knows or cares whether meals
come from a deterministic mock or a real browser driving DoorDash.

A provider does two things and nothing else:
  - discover(config) -> list[Candidate]   (read the world; never spends)
  - place_order(candidate, ...) -> OrderResult

The engine (engine/decision.py) owns every safety/budget verdict. A provider
only surfaces options and carries out an *already-approved* placement, and even
then it stops before money moves unless explicitly and separately authorized.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from engine.models import Candidate, UserConfig


class OrderStatus(str, Enum):
    """Terminal outcome of a placement attempt."""

    PLACED = "PLACED"  # mock-only: a simulated confirmation (no real charge)
    STOPPED_BEFORE_PAYMENT = "STOPPED_BEFORE_PAYMENT"  # real adapter success: carted, halted at the pay gate
    SKIPPED = "SKIPPED"  # nothing placed by design (e.g. decision was not AUTO)
    FAILED = "FAILED"  # provider error mid-flight (timeout, selector miss)
    BLOCKED = "BLOCKED"  # provider could not proceed safely (bot wall, not logged in)


class ProviderError(RuntimeError):
    """Base class for provider failures the run loop should record, not crash on."""


class ProviderUnavailable(ProviderError):
    """The provider could not be reached/used: bot wall, no login, no address.

    This is an *expected* failure path, not a bug — the run loop classifies it
    and notifies, it never silently proceeds.
    """


@dataclass(frozen=True)
class OrderResult:
    """Structured outcome of place_order — the receipt the run loop records.

    `charged` is the load-bearing safety field: it is False for every code path
    in this build. Nothing flips it to True without a separate, explicit,
    human-confirmed payment flag (see DoorDashProvider).
    """

    status: OrderStatus
    provider: str
    restaurant: str
    item_name: str
    price_usd: float | None
    idempotency_key: str
    reason: str = ""
    charged: bool = False
    summary: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "provider": self.provider,
            "restaurant": self.restaurant,
            "item_name": self.item_name,
            "price_usd": self.price_usd,
            "idempotency_key": self.idempotency_key,
            "reason": self.reason,
            "charged": self.charged,
            "summary": self.summary,
            "screenshot_path": self.screenshot_path,
        }


@runtime_checkable
class Provider(Protocol):
    """The interface both MockProvider and DoorDashProvider implement."""

    name: str

    def discover(self, config: UserConfig) -> list[Candidate]:
        """Return candidate meals for the user, given their preferences."""
        ...

    def discover_fallback(self, config: UserConfig) -> list[Candidate]:
        """Return candidates from the user's pre-vetted fallback restaurant.

        Used when the primary selection is BLOCKed: the run loop re-checks these
        for safety + budget and, if one passes, treats it as a fallback-in-use
        CONFIRM. Returns [] when no fallback is configured.
        """
        ...

    def place_order(
        self,
        candidate: Candidate,
        *,
        idempotency_key: str,
        complete_payment: bool = False,
        budget_ceiling_usd: float | None = None,
    ) -> OrderResult:
        """Carry out an already-approved placement, stopping before payment.

        budget_ceiling_usd, when given, is re-checked against the real checkout
        total before success is reported (fail closed if over / unverifiable).
        """
        ...


__all__ = [
    "OrderResult",
    "OrderStatus",
    "Provider",
    "ProviderError",
    "ProviderUnavailable",
]
