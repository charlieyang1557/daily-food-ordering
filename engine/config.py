from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from engine.models import (
    BudgetConfig,
    FallbackConfig,
    NotificationsConfig,
    PreferencesConfig,
    RestrictionsConfig,
    ScheduleConfig,
    UserConfig,
)


ALLOWED_DIETARY = {
    "vegetarian",
    "vegan",
    "halal",
    "kosher",
    "pescatarian",
    "gluten-free",
    "dairy-free",
}

ALLOWED_ALLERGENS = {
    "milk",
    "eggs",
    "fish",
    "shellfish",
    "tree nuts",
    "peanuts",
    "wheat",
    "soy",
    "sesame",
}

KNOWN_INGREDIENTS = ALLOWED_ALLERGENS | {
    "beef",
    "chicken",
    "cilantro",
    "garlic",
    "ginger",
    "lamb",
    "mushroom",
    "onion",
    "pork",
    "shrimp",
    "tofu",
}

ORDER_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class ConfigError(ValueError):
    """Raised when user preferences cannot be used safely."""


def load_config(path: str | Path = "user_preferences.yaml") -> UserConfig:
    with Path(path).open("r", encoding="utf-8") as file:
        raw = yaml.safe_load(file) or {}
    if not isinstance(raw, Mapping):
        raise ConfigError("config root must be a mapping")
    return load_config_from_dict(raw)


def load_config_from_dict(raw: Mapping[str, Any]) -> UserConfig:
    budget_raw = _section(raw, "budget")
    budget = _load_budget(budget_raw)

    schedule = _load_schedule(_section(raw, "schedule"))
    preferences = PreferencesConfig(
        cuisines=_string_list(_section(raw, "preferences").get("cuisines")),
        favorite_restaurants=_string_list(
            _section(raw, "preferences").get("favorite_restaurants")
        ),
        dislikes=_string_list(_section(raw, "preferences").get("dislikes")),
    )
    restrictions = _load_restrictions(_section(raw, "restrictions"))
    fallback = _load_fallback(raw.get("fallback"))
    notifications = _load_notifications(_section(raw, "notifications"))

    return UserConfig(
        budget=budget,
        schedule=schedule,
        preferences=preferences,
        restrictions=restrictions,
        fallback=fallback,
        notifications=notifications,
        clear_cart=_as_bool(_first(raw, "clear_cart", default=False), "clear_cart"),
    )


def _load_budget(raw: Mapping[str, Any]) -> BudgetConfig:
    daily_max = _first(raw, "daily_max_usd", "daily_max")
    if daily_max is None:
        raise ConfigError("budget.daily_max_usd is required")

    daily_max_usd = _positive_money(daily_max, "budget.daily_max_usd")
    auto_approve_under_usd = _non_negative_money(
        _first(raw, "auto_approve_under_usd", "auto_approve_under", default=0),
        "budget.auto_approve_under_usd",
    )
    if auto_approve_under_usd > daily_max_usd:
        raise ConfigError("budget.auto_approve_under_usd cannot exceed daily_max_usd")

    rolling_cap_value = _first(raw, "rolling_cap_usd", "rolling_cap")
    rolling_cap_usd = None
    if rolling_cap_value is not None:
        rolling_cap_usd = _positive_money(rolling_cap_value, "budget.rolling_cap_usd")

    rolling_window_days = int(raw.get("rolling_window_days", 7))
    if rolling_window_days <= 0:
        raise ConfigError("budget.rolling_window_days must be positive")

    return BudgetConfig(
        daily_max_usd=daily_max_usd,
        auto_approve_under_usd=auto_approve_under_usd,
        rolling_cap_usd=rolling_cap_usd,
        rolling_window_days=rolling_window_days,
    )


def _load_schedule(raw: Mapping[str, Any]) -> ScheduleConfig:
    order_time = str(raw.get("order_time", "12:00"))
    if not ORDER_TIME_RE.match(order_time):
        raise ConfigError("schedule.order_time must use HH:MM 24-hour format")

    timezone = str(raw.get("timezone", "UTC"))
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ConfigError(f"schedule.timezone is not a valid IANA timezone: {timezone}") from error

    return ScheduleConfig(order_time=order_time, timezone=timezone)


def _load_restrictions(raw: Mapping[str, Any]) -> RestrictionsConfig:
    dietary = _normalized_list(raw.get("dietary"))
    unknown_dietary = sorted(set(dietary) - ALLOWED_DIETARY)
    if unknown_dietary:
        raise ConfigError(f"unsupported dietary restriction: {', '.join(unknown_dietary)}")

    allergies = _normalized_list(raw.get("allergies"))
    unknown_allergies = sorted(set(allergies) - ALLOWED_ALLERGENS)
    if unknown_allergies:
        raise ConfigError(f"unsupported allergen: {', '.join(unknown_allergies)}")

    never_order = _string_list(raw.get("never_order"))
    ingredient_entries = [
        value for value in never_order if value.strip().lower() in KNOWN_INGREDIENTS
    ]
    if ingredient_entries:
        raise ConfigError(
            "restrictions.never_order only accepts restaurants or cuisines; "
            f"move ingredient dislikes elsewhere: {', '.join(ingredient_entries)}"
        )

    return RestrictionsConfig(
        dietary=dietary,
        allergies=allergies,
        never_order=never_order,
    )


def _load_fallback(raw: Any) -> FallbackConfig:
    if raw is None:
        return FallbackConfig()
    if isinstance(raw, str):
        return FallbackConfig(restaurant=raw)
    if isinstance(raw, Mapping):
        restaurant = raw.get("restaurant")
        return FallbackConfig(restaurant=str(restaurant) if restaurant else None)
    raise ConfigError("fallback must be a string or mapping")


def _load_notifications(raw: Mapping[str, Any]) -> NotificationsConfig:
    channel = raw.get("channel")
    timeout = int(raw.get("confirmation_timeout_minutes", 20))
    if timeout <= 0:
        raise ConfigError("notifications.confirmation_timeout_minutes must be positive")
    return NotificationsConfig(
        channel=str(channel) if channel else None,
        confirmation_timeout_minutes=timeout,
    )


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    section = raw.get(name, {})
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ConfigError(f"{name} must be a mapping")
    return section


def _first(raw: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return default


def _as_bool(value: Any, field_name: str) -> bool:
    """Strict boolean for a config flag — never `bool()` coercion (where the string
    "false" is truthy). A real YAML bool passes through; an explicit true/false token
    is honored; anything ambiguous is rejected loudly. The general config-bool path:
    its first caller is the DESTRUCTIVE clear_cart flag (where failing SAFE matters),
    and any future boolean field should reuse it unchanged."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    token = str(value).strip().lower()
    if token in ("true", "yes", "on", "1"):
        return True
    if token in ("false", "no", "off", "0", ""):
        return False
    raise ConfigError(f"{field_name} must be true or false (got {value!r})")


def _positive_money(value: Any, field_name: str) -> float:
    result = _money(value, field_name)
    if result <= 0:
        raise ConfigError(f"{field_name} must be positive")
    return result


def _non_negative_money(value: Any, field_name: str) -> float:
    result = _money(value, field_name)
    if result < 0:
        raise ConfigError(f"{field_name} cannot be negative")
    return result


def _money(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(f"{field_name} must be a number") from error
    return result


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ConfigError("expected a list of strings")
    return [str(item) for item in value]


def _normalized_list(value: Any) -> list[str]:
    return [item.strip().lower() for item in _string_list(value) if item.strip()]


parse_config = load_config_from_dict
load_user_config = load_config

__all__ = [
    "ALLOWED_ALLERGENS",
    "ALLOWED_DIETARY",
    "ConfigError",
    "load_config",
    "load_config_from_dict",
    "load_user_config",
    "parse_config",
]
