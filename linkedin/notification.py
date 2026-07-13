# linkedin/notification.py
"""Telegram notifications for pipeline failures."""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass

from linkedin.conf import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


@dataclass
class FailureEvent:
    """A structured failure event sent to Telegram."""
    title: str
    detail: str
    campaign: str | None = None
    task_type: str | None = None
    lead: str | None = None


def send_telegram(message: str) -> bool:
    """Send a plain-text message to the configured Telegram chat.

    Returns ``True`` on success, ``False`` on failure (logged).
    This is best-effort — never raises.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                logger.warning("Telegram API returned not-ok: %s", body)
                return False
            return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        logger.debug("Telegram notification failed: %s", exc)
        return False


def notify_failure(event: FailureEvent) -> bool:
    """Format and send a failure notification to Telegram.

    The message is structured as:

    ❌ <title>
    Campaign: <campaign>
    Detail: <detail>

    Optional fields (task_type, lead) are appended when present.
    """
    parts = [f"\u274c <b>{event.title}</b>"]

    if event.campaign:
        parts.append(f"\ud83d\udce6 Campaign: {event.campaign}")
    if event.task_type:
        parts.append(f"\u2699\ufe0f Task: {event.task_type}")
    if event.lead:
        parts.append(f"\ud83d\udc64 Lead: {event.lead}")

    parts.append(f"\ud83d\udcdd {event.detail}")

    return send_telegram("\n".join(parts))
