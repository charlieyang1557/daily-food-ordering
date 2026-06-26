"""Optional, self-contained Discord notifier for the daily-food-ordering skill.

An alternative to OpenClaw's `message` tool: it posts a one-line,
severity-calibrated summary of a run straight to a Discord webhook. The webhook
URL is a SECRET — it is read from the DFO_DISCORD_WEBHOOK environment variable
and is NEVER stored in the repo. Best-effort: a failed notification never
crashes a run.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

WEBHOOK_ENV = "DFO_DISCORD_WEBHOOK"

_EMOJI = {"AUTO": "🍱", "CONFIRM": "🤔", "BLOCK": "🚫"}


def format_notification(result: dict[str, Any]) -> str:
    """Build a one-line Discord summary from a RunResult.to_dict()."""
    if result.get("already_ran"):
        return "ℹ️ Daily Food Ordering — already ran today; skipped."

    decision = result.get("decision") or {}
    status = decision.get("decision")
    reason = decision.get("reason", "")
    severity = decision.get("severity", "")
    selected = result.get("selected_candidate") or {}
    item, restaurant, price = selected.get("item_name"), selected.get("restaurant"), selected.get("price_usd")
    order = result.get("order_result") or {}
    order_status = order.get("status")
    summary = order.get("summary") or {}
    emoji = _EMOJI.get(status, "ℹ️")

    if status == "AUTO":
        if order_status == "PLACED":
            return f"{emoji} Ordered **{item}** from {restaurant} — ${price} (auto, within budget)."
        if order_status == "STOPPED_BEFORE_PAYMENT":
            total = summary.get("checkout_total_usd")
            note = f" (substituted for {summary['substituted_for']})" if summary.get("substituted_for") else ""
            return (f"{emoji} Carted **{order.get('item_name')}**{note} from {order.get('restaurant')} "
                    f"on DoorDash and **stopped before payment** — ${total} total. No charge made.")
        if order_status in ("FAILED", "BLOCKED"):
            return f"⚠️ Couldn't complete **{item}**: {(order.get('reason') or '')[:90]}. No charge made."
        return f"{emoji} {item} from {restaurant} — AUTO."
    if status == "CONFIRM":
        return (f"{emoji} **Confirm needed** ({reason}): {item} from {restaurant} — ${price}. "
                f"Approve to order, or it is skipped on silence.")
    if status == "BLOCK":
        return f"🚫 **Skipped today** ({reason} · {severity}). Nothing ordered."
    return f"ℹ️ Daily Food Ordering ran ({status})."


def notify_discord(message: str, *, webhook_url: str | None = None,
                   username: str = "Daily Food Ordering") -> bool:
    """POST `message` to a Discord webhook. Returns True on success.

    webhook_url defaults to the DFO_DISCORD_WEBHOOK env var. Never raises — a
    notification failure must not crash the run.
    """
    url = webhook_url or os.environ.get(WEBHOOK_ENV)
    if not url:
        return False
    payload = json.dumps({"username": username, "content": message[:1900]}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            # Discord/Cloudflare blocks the default "Python-urllib" UA.
            "User-Agent": "daily-food-ordering/1.0 (+https://github.com/charlieyang1557/daily-food-ordering)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= getattr(response, "status", response.getcode()) < 300
    except Exception:  # noqa: BLE001
        return False


__all__ = ["format_notification", "notify_discord", "WEBHOOK_ENV"]
