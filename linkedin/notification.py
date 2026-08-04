# linkedin/notification.py
"""Telegram notifications for pipeline failures."""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from dataclasses import dataclass

from linkedin.conf import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    OPPORTUNITY_BOT_TOKEN,
    OPPORTUNITY_CHAT_ID,
)

logger = logging.getLogger(__name__)


@dataclass
class FailureEvent:
    """A structured failure event sent to Telegram."""
    title: str
    detail: str
    campaign: str | None = None
    task_type: str | None = None
    lead: str | None = None


def send_telegram(
    message: str,
    *,
    bot_token: str | None = None,
    chat_id: str | None = None,
) -> bool:
    """Send a plain-text message to a Telegram chat.

    Defaults to the general alert bot (``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID``).
    Pass ``bot_token``/``chat_id`` to send via a different bot (e.g. the
    opportunity bot). Returns ``True`` on success, ``False`` on failure
    (logged). Best-effort — never raises.
    """
    token = bot_token or TELEGRAM_BOT_TOKEN
    chat = chat_id or TELEGRAM_CHAT_ID
    if not token or not chat:
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat,
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


def notify_opportunity(
    campaign: str,
    lead: str,
    opportunity_score: float,
    threshold: float,
    notify_recommended: bool,
    opportunity_type: str,
    summary: str,
    evidence: list[str],
) -> bool:
    """Send an opportunity alert notification via Telegram.

    Triggered when an opportunity assessment exceeds the configured threshold
    or when ``notify_recommended`` is true.
    """
    reasons = []
    if notify_recommended:
        reasons.append("Agent recommended human attention")
    if opportunity_score >= threshold:
        reasons.append(f"Opportunity score {opportunity_score:.2f} ≥ threshold {threshold}")

    parts = [
        "\U0001f680 <b>Opportunity Detected</b>",
        f"\U0001f4e6 <b>Campaign:</b> {campaign}",
        f"\U0001f464 <b>Lead:</b> {lead}",
        "",
        f"\U0001f3af Opportunity Score: <b>{opportunity_score:.2f}</b> (threshold: {threshold})",
        f"\U0001f4ac Notify Recommended: {'Yes' if notify_recommended else 'No'}",
        f"\u26a1 Triggered: {' | '.join(reasons)}",
        "",
        f"\U0001f4cb Type: {opportunity_type}",
        f"\U0001f4dd Summary: {summary}",
    ]

    if evidence:
        parts.append("")
        parts.append("\U0001f50d Evidence:")
        for e in evidence[:5]:  # limit to 5 evidence items
            parts.append(f"  \u2022 {e}")
        if len(evidence) > 5:
            parts.append(f"  (+{len(evidence) - 5} more)")

    return send_telegram(
        "\n".join(parts),
        bot_token=OPPORTUNITY_BOT_TOKEN,
        chat_id=OPPORTUNITY_CHAT_ID,
    )


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
