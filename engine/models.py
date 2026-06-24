from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping


class DecisionStatus(str, Enum):
    AUTO = "AUTO"
    CONFIRM = "CONFIRM"
    BLOCK = "BLOCK"


class Severity(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


@dataclass
class ScheduleConfig:
    order_time: str = "12:00"
    timezone: str = "UTC"


@dataclass
class BudgetConfig:
    daily_max_usd: float
    auto_approve_under_usd: float = 0
    rolling_cap_usd: float | None = None
    rolling_window_days: int = 7

    @property
    def daily_max(self) -> float:
        return self.daily_max_usd

    @property
    def auto_approve_under(self) -> float:
        return self.auto_approve_under_usd

    @property
    def rolling_cap(self) -> float | None:
        return self.rolling_cap_usd


@dataclass
class PreferencesConfig:
    cuisines: list[str] = field(default_factory=list)
    favorite_restaurants: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)


@dataclass
class RestrictionsConfig:
    dietary: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    never_order: list[str] = field(default_factory=list)


@dataclass
class FallbackConfig:
    restaurant: str | None = None


@dataclass
class NotificationsConfig:
    channel: str | None = None
    confirmation_timeout_minutes: int = 20


@dataclass
class UserConfig:
    budget: BudgetConfig
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    preferences: PreferencesConfig = field(default_factory=PreferencesConfig)
    restrictions: RestrictionsConfig = field(default_factory=RestrictionsConfig)
    fallback: FallbackConfig = field(default_factory=FallbackConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)


@dataclass(init=False)
class Candidate:
    restaurant: str
    item_name: str
    price_usd: float
    cuisine: str | None
    dietary: list[str]
    allergens: list[str]
    verified_safe: bool
    available: bool
    metadata: dict[str, Any]

    def __init__(
        self,
        restaurant: str,
        item_name: str | None = None,
        price_usd: float | None = None,
        *,
        item: str | None = None,
        price: float | None = None,
        cuisine: str | None = None,
        dietary: Iterable[str] | None = None,
        allergens: Iterable[str] | None = None,
        verified_safe: bool = False,
        available: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if item_name is None:
            item_name = item or ""
        if price_usd is None:
            price_usd = price
        if price_usd is None:
            raise TypeError("Candidate requires price_usd or price")

        self.restaurant = restaurant
        self.item_name = item_name
        self.price_usd = float(price_usd)
        self.cuisine = cuisine
        self.dietary = list(dietary or [])
        self.allergens = list(allergens or [])
        self.verified_safe = verified_safe
        self.available = available
        self.metadata = dict(metadata or {})

    @property
    def name(self) -> str:
        return self.item_name

    @property
    def price(self) -> float:
        return self.price_usd

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Candidate":
        return cls(
            restaurant=str(value.get("restaurant", "")),
            item_name=value.get("item_name") or value.get("item") or value.get("name"),
            price_usd=value.get("price_usd", value.get("price")),
            cuisine=value.get("cuisine"),
            dietary=value.get("dietary"),
            allergens=value.get("allergens"),
            verified_safe=bool(value.get("verified_safe", False)),
            available=bool(value.get("available", True)),
            metadata=value.get("metadata"),
        )


@dataclass
class DecisionResult:
    decision: DecisionStatus
    reason: str
    severity: Severity
    candidate: Candidate | None = None

    @property
    def status(self) -> DecisionStatus:
        return self.decision


Config = UserConfig
OrderCandidate = Candidate
Decision = DecisionResult

AUTO = DecisionStatus.AUTO
CONFIRM = DecisionStatus.CONFIRM
BLOCK = DecisionStatus.BLOCK

__all__ = [
    "AUTO",
    "BLOCK",
    "CONFIRM",
    "BudgetConfig",
    "Candidate",
    "Config",
    "Decision",
    "DecisionResult",
    "DecisionStatus",
    "FallbackConfig",
    "NotificationsConfig",
    "OrderCandidate",
    "PreferencesConfig",
    "RestrictionsConfig",
    "ScheduleConfig",
    "Severity",
    "UserConfig",
]
