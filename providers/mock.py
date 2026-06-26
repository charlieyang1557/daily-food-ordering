"""Deterministic provider — the test/dry-run stand-in for a real platform.

It carries the v1 walking-skeleton candidate (so the run-pipeline test stays
pinned) and adds named failure scenarios so every guardrail in the engine can
be exercised on demand without a network or a browser:

    happy        one safe, in-budget candidate            -> AUTO
    over_budget  one candidate priced above daily_max      -> BLOCK over_daily_max
    unavailable  one candidate marked sold out             -> BLOCK unavailable
    empty        no candidates at all                      -> BLOCK no_candidate
    allergen     one candidate carrying a flagged allergen -> BLOCK allergy_violation
"""
from __future__ import annotations

from engine.models import Candidate, UserConfig
from providers.base import OrderResult, OrderStatus

SCENARIOS = ("happy", "over_budget", "unavailable", "empty", "allergen")


class MockProvider:
    name = "mock"

    def __init__(self, scenario: str = "happy") -> None:
        if scenario not in SCENARIOS:
            raise ValueError(
                f"unknown mock scenario {scenario!r}; choose from {', '.join(SCENARIOS)}"
            )
        self.scenario = scenario

    def discover(self, config: UserConfig) -> list[Candidate]:
        dietary = list(config.restrictions.dietary)
        if self.scenario == "empty":
            return []
        if self.scenario == "over_budget":
            return [
                Candidate(
                    restaurant="Gold Leaf Omakase",
                    item_name="Chef's Tasting Menu",
                    price_usd=99,
                    cuisine="Japanese",
                    dietary=dietary,
                    allergens=[],
                    verified_safe=True,
                )
            ]
        if self.scenario == "unavailable":
            return [
                Candidate(
                    restaurant="Thai Spice",
                    item_name="Vegetarian Pad Thai",
                    price_usd=14,
                    cuisine="Thai",
                    dietary=dietary,
                    allergens=[],
                    verified_safe=True,
                    available=False,
                )
            ]
        if self.scenario == "allergen":
            return [
                Candidate(
                    restaurant="Satay House",
                    item_name="Peanut Noodles",
                    price_usd=13,
                    cuisine="Thai",
                    dietary=dietary,
                    allergens=["peanuts"],
                    verified_safe=True,
                )
            ]
        # happy
        return [
            Candidate(
                restaurant="Thai Spice",
                item_name="Vegetarian Pad Thai",
                price_usd=14,
                cuisine="Thai",
                dietary=dietary,
                allergens=[],
                verified_safe=True,
            )
        ]

    def discover_fallback(self, config: UserConfig) -> list[Candidate]:
        # The pre-vetted fallback: safe (verified), restriction-compliant, cheap.
        if not config.fallback.restaurant:
            return []
        return [
            Candidate(
                restaurant=config.fallback.restaurant,
                item_name="Veggie Burrito Bowl",
                price_usd=11,
                cuisine="Mexican",
                dietary=list(config.restrictions.dietary),
                allergens=[],
                verified_safe=True,
            )
        ]

    def place_order(
        self,
        candidate: Candidate,
        *,
        idempotency_key: str,
        complete_payment: bool = False,
        budget_ceiling_usd: float | None = None,
    ) -> OrderResult:
        # The mock simulates a confirmed order. It never touches a real account,
        # so `charged` stays False even if a caller passes complete_payment.
        return OrderResult(
            status=OrderStatus.PLACED,
            provider=self.name,
            restaurant=candidate.restaurant,
            item_name=candidate.item_name,
            price_usd=candidate.price_usd,
            idempotency_key=idempotency_key,
            reason="mock_simulated_confirmation",
            charged=False,
            summary={"simulated": True, "note": "no real platform contacted"},
        )


__all__ = ["MockProvider", "SCENARIOS"]
