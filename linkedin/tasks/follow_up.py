# linkedin/tasks/follow_up.py
"""DEPRECATED — follow-up logic merged into check_messages.py.

This file retains shared constants and helpers imported by the unified
``check_messages`` handler. See ``BACKUP_follow_up.py`` at the project
root for the original standalone implementation.

The ``handle_follow_up`` function and ``enqueue_follow_up`` are gone;
proactive nudges and lead replies are both handled by
``handle_check_messages`` in ``check_messages.py``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Required silence between nudges scales with unanswered count:
# 1 unanswered → 3d, 2 → 6d, 3 → 9d. Skips the LLM call while open.
MIN_DAYS_PER_UNANSWERED = 3

# Maximum number of consecutive unanswered follow-ups before auto-completing
MAX_UNANSWERED_FOLLOW_UPS = 3

# Minimum hours between follow-up messages (safety floor — LLM should already
# respect this via the prompt, but we enforce it at the code level).
# The prompt instructs 2-8h for active conversations and 24-48h for async.
MIN_FOLLOW_UP_HOURS = 2

TIMESTAMP_TOLERANCE_SECONDS = 120


def _build_send_profile(deal) -> dict:
    """Minimal profile dict for ``send_raw_message`` and its fallbacks."""
    lead = deal.lead
    return {
        "public_identifier": lead.public_identifier,
        "urn": lead.urn or "",
    }


def _has_manual_messages_recently(deal, session) -> bool:
    """Detect manual messages by checking if the last outgoing message has a
    matching ActionLog nearby in time.

    Only outgoing messages (``is_outgoing=True`` — messages *we* sent) are
    compared.  Lead replies are ignored.

    If the last outgoing message's timestamp has NO FOLLOW_UP ActionLog
    within ``TIMESTAMP_TOLERANCE_SECONDS``, it was sent manually.
    """
    from datetime import timedelta

    from django.contrib.contenttypes.models import ContentType

    from chat.models import ChatMessage
    from linkedin.models import ActionLog

    ct = ContentType.objects.get_for_model(type(deal.lead))

    last_outgoing = (
        ChatMessage.objects.filter(
            content_type=ct,
            object_id=deal.lead_id,
            is_outgoing=True,
        )
        .order_by("-creation_date", "-pk")
        .first()
    )

    if last_outgoing is None:
        return False

    window_start = last_outgoing.creation_date - timedelta(
        seconds=TIMESTAMP_TOLERANCE_SECONDS,
    )
    window_end = last_outgoing.creation_date + timedelta(
        seconds=TIMESTAMP_TOLERANCE_SECONDS,
    )

    nearby = ActionLog.objects.filter(
        linkedin_profile=session.linkedin_profile,
        action_type=ActionLog.ActionType.FOLLOW_UP,
        created_at__gte=window_start,
        created_at__lte=window_end,
    ).exists()

    if not nearby:
        logger.debug(
            "Manual message detected for %s: last outgoing at %s, "
            "no FOLLOW_UP ActionLog within %ds",
            deal.lead.public_identifier,
            last_outgoing.creation_date,
            TIMESTAMP_TOLERANCE_SECONDS,
        )

    return not nearby


def _notify_manual_intervention(session, deal, public_id: str) -> None:
    """Log a warning that AI follow-ups have been paused due to manual
    intervention, with a preview of the manual message."""
    from django.contrib.contenttypes.models import ContentType

    from chat.models import ChatMessage

    try:
        ct = ContentType.objects.get_for_model(type(deal.lead))

        last_manual = (
            ChatMessage.objects.filter(
                content_type=ct,
                object_id=deal.lead_id,
                is_outgoing=True,
            )
            .order_by("-creation_date")
            .first()
        )

        lead_name = deal.lead.public_identifier
        message_preview = ""
        if last_manual:
            message_preview = (
                last_manual.content[:100] + "..."
                if len(last_manual.content) > 100
                else last_manual.content
            )

        logger.warning(
            "\U0001f916 Manual message detected - skipping follow-up for 7d"
            " (%s)\n"
            'Message: "%s"',
            lead_name,
            message_preview,
        )

    except Exception as e:
        logger.error(
            "Failed to send manual intervention notification for %s \u2192 %s",
            public_id,
            e,
        )
