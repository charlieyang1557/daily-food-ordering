"""Unit tests for the DoorDash adapter's safety-critical *pure* logic.

The browser-driving methods (discover / place_order) are exercised live behind
the DD_LIVE env flag (see test_doordash_live.py). Here we pin the logic that
keeps a real account safe — bot-wall detection, price parsing, and above all
the payment gate, which must refuse to charge by default and even under the
flag never actually completes a charge in this build.
"""
import pytest

from providers.base import OrderResult, OrderStatus
from providers.doordash import DoorDashProvider


@pytest.mark.parametrize(
    "title,body",
    [
        ("Just a moment...", "verifying you are human"),
        ("DoorDash", "Performing security verification. Ray ID: abc"),
        ("Access denied", "px-captcha please complete"),
    ],
)
def test_bot_wall_is_detected(title, body):
    assert DoorDashProvider.is_bot_walled(title, body) is True


def test_normal_page_is_not_bot_walled():
    assert DoorDashProvider.is_bot_walled("Order food", "Add to cart — $12.50") is False


@pytest.mark.parametrize(
    "text,expected",
    [
        ("$14.50", 14.50),
        ("$1,234.00", 1234.00),
        ("$9", 9.0),
        ("USD 21.99", 21.99),
        ("Free", 0.0),
        ("", None),
        ("no price here", None),
        # a rating must NOT be read as the price (budget-gate integrity)
        ("Pad Thai\n4.5 ★ (120)\n$14.50", 14.50),
        # "free" inside a dish name must NOT zero a real price
        ("Sugar-Free Soda $3.50", 3.50),
        # a leading $0 promo must NOT mask the real price
        ("$0 delivery\nPad Thai\n$14.50", 14.50),
    ],
)
def test_parse_price(text, expected):
    assert DoorDashProvider.parse_price(text) == expected


def test_payment_is_not_authorized_by_default():
    # No flag -> never even consider charging.
    assert DoorDashProvider._payment_authorized(complete_payment=False) is False


def test_payment_flag_alone_is_insufficient(monkeypatch):
    monkeypatch.delenv("DAILY_FOOD_CONFIRM_CHARGE", raising=False)
    # The flag without the explicit human-typed env confirmation is not enough.
    assert DoorDashProvider._payment_authorized(complete_payment=True) is False


def test_payment_requires_flag_and_typed_env_confirmation(monkeypatch):
    monkeypatch.setenv("DAILY_FOOD_CONFIRM_CHARGE", "I UNDERSTAND THIS CHARGES MY CARD")
    assert DoorDashProvider._payment_authorized(complete_payment=True) is True
    # ...but the flag without env still fails, and env without flag still fails.
    assert DoorDashProvider._payment_authorized(complete_payment=False) is False


def test_build_stopped_result_is_never_charged():
    from engine.models import Candidate

    candidate = Candidate("Thai Spice", "Veg Pad Thai", 14, cuisine="Thai")
    summary = {"subtotal": "$14.00", "total": "$18.40", "items": ["Veg Pad Thai"]}
    result = DoorDashProvider()._build_stopped_result(
        candidate, idempotency_key="k-1", summary=summary, screenshot_path="/tmp/x.png"
    )
    assert isinstance(result, OrderResult)
    assert result.status is OrderStatus.STOPPED_BEFORE_PAYMENT
    assert result.charged is False
    assert result.provider == "doordash"
    assert result.summary["total"] == "$18.40"
    assert result.screenshot_path == "/tmp/x.png"
