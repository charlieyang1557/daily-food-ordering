"""Tests for the optional Discord notifier — message formatting (pure, no network)
and the no-webhook safety of notify_discord.
"""
from notify import format_notification, notify_discord


def _result(decision_status, reason="", *, order_status=None, item="Pad Thai",
            restaurant="Thai Spice", price=14, summary=None, already_ran=False, severity="P2"):
    return {
        "already_ran": already_ran,
        "selected_candidate": {"item_name": item, "restaurant": restaurant, "price_usd": price},
        "decision": {"decision": decision_status, "reason": reason, "severity": severity} if decision_status else None,
        "order_result": ({"status": order_status, "item_name": item, "restaurant": restaurant,
                          "reason": reason, "summary": summary or {}} if order_status else None),
    }


def test_auto_placed_message():
    msg = format_notification(_result("AUTO", "within_auto_approve", order_status="PLACED"))
    assert "Ordered" in msg and "Pad Thai" in msg and "$14" in msg


def test_auto_stopped_before_payment_message():
    msg = format_notification(_result(
        "AUTO", order_status="STOPPED_BEFORE_PAYMENT",
        summary={"checkout_total_usd": 27.68, "substituted_for": "A10 Curry"}))
    assert "stopped before payment" in msg.lower()
    assert "No charge made" in msg
    assert "substituted for A10 Curry" in msg


def test_confirm_message_asks_for_approval():
    msg = format_notification(_result("CONFIRM", "above_auto_approve", price=21, severity="P1"))
    assert "Confirm needed" in msg and "above_auto_approve" in msg


def test_block_message_orders_nothing():
    msg = format_notification(_result("BLOCK", "over_daily_max", severity="P1"))
    assert "Skipped today" in msg and "over_daily_max" in msg and "Nothing ordered" in msg


def test_already_ran_message():
    assert "already ran" in format_notification(_result("AUTO", already_ran=True)).lower()


def test_failed_message_notes_no_charge():
    msg = format_notification(_result("AUTO", "TimeoutError", order_status="FAILED"))
    assert "No charge made" in msg


def test_notify_discord_without_webhook_is_a_safe_noop(monkeypatch):
    monkeypatch.delenv("DFO_DISCORD_WEBHOOK", raising=False)
    assert notify_discord("hello") is False  # no URL -> no post, no raise
