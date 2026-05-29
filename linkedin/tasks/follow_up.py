# linkedin/tasks/follow_up.py
"""Follow-up task — runs the agentic follow-up for one CONNECTED profile."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone
from termcolor import colored

from linkedin.models import ActionLog

logger = logging.getLogger(__name__)

# Required silence between nudges scales with unanswered count:
# 1 unanswered → 3d, 2 → 6d, 3 → 9d. Skips the LLM call while open.
MIN_DAYS_PER_UNANSWERED = 3

# Maximum number of consecutive unanswered follow-ups before auto-completing
MAX_UNANSWERED_FOLLOW_UPS = 3


def _build_send_profile(deal) -> dict:
    """Minimal profile dict for ``send_raw_message`` and its fallbacks.

    Populated from the Lead row — all three send strategies (popup,
    direct-thread, API) now navigate by URN so no human-readable name
    is required.
    """
    lead = deal.lead
    return {
        "public_identifier": lead.public_identifier,
        "urn": lead.urn or "",
    }


def _too_soon_to_nudge(deal) -> bool:
    """Wait `unanswered_count * MIN_DAYS_PER_UNANSWERED` days between nudges."""
    from django.contrib.contenttypes.models import ContentType

    from chat.models import ChatMessage

    ct = ContentType.objects.get_for_model(type(deal.lead))
    messages = ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id)

    last = messages.order_by("-creation_date").first()
    if last is None or not last.is_outgoing:
        return False

    last_reply = messages.filter(is_outgoing=False).order_by("-creation_date").first()
    nudges = messages.filter(is_outgoing=True)
    if last_reply:
        nudges = nudges.filter(creation_date__gt=last_reply.creation_date)

    required = timedelta(days=nudges.count() * MIN_DAYS_PER_UNANSWERED)
    return timezone.now() - last.creation_date < required


TIMESTAMP_TOLERANCE_SECONDS = 120


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

    ct = ContentType.objects.get_for_model(type(deal.lead))

    # Get the most recent outgoing message (sent by our account — could
    # be AI or manual).  Lead replies (is_outgoing=False) are irrelevant.
    last_outgoing = (
        ChatMessage.objects.filter(
            content_type=ct,
            object_id=deal.lead_id,
            is_outgoing=True,
        )
        .order_by("-creation_date", "-pk")
        .first()
    )

    # No outgoing messages at all -> nothing to detect
    if last_outgoing is None:
        return False

    # Check if there's an ActionLog within TIMESTAMP_TOLERANCE_SECONDS of
    # the last outgoing message's creation_date.  If yes, the message was
    # sent by the AI (which always creates an ActionLog).  If not, someone
    # sent it manually outside the system.
    window_start = last_outgoing.creation_date - timedelta(
        seconds=TIMESTAMP_TOLERANCE_SECONDS
    )
    window_end = last_outgoing.creation_date + timedelta(
        seconds=TIMESTAMP_TOLERANCE_SECONDS
    )

    nearby = (
        ActionLog.objects.filter(
            linkedin_profile=session.linkedin_profile,
            campaign=deal.campaign,
            action_type=ActionLog.ActionType.FOLLOW_UP,
            created_at__gte=window_start,
            created_at__lte=window_end,
        ).exists()
    )

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
            "\U0001f916 AI PAUSED: Manual message detected in conversation"
            " with %s\n"
            'Message: "%s"\n'
            "Campaign: %s\n"
            "To resume AI follow-ups, use Django Admin or run: "
            "python manage.py resume_follow_up %s %s",
            lead_name,
            message_preview,
            session.campaign.name,
            session.campaign.pk,
            lead_name,
        )

    except Exception as e:
        logger.error(
            "Failed to send manual intervention notification for %s → %s",
            public_id,
            e,
        )


def handle_follow_up(task, session, qualifiers):
    from crm.models import Deal
    from linkedin.actions.message import send_raw_message
    from linkedin.agents.follow_up import run_follow_up_agent
    from linkedin.db.deals import set_profile_state
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.enums import ProfileState
    from linkedin.tasks.scheduler import enqueue_follow_up

    payload = task.payload
    public_id = payload["public_id"]
    campaign_id = payload["campaign_id"]

    logger.info(
        "[%s] %s %s",
        session.campaign,
        colored("\u25b6 follow_up", "green", attrs=["bold"]),
        public_id,
    )

    # Rate limit check
    if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        enqueue_follow_up(campaign_id, public_id, delay_seconds=3600)
        return

    deal = (
        Deal.objects.filter(
            lead__public_identifier=public_id, campaign=session.campaign
        )
        .select_related("lead", "campaign")
        .first()
    )
    if deal is None:
        logger.warning("follow_up: no Deal for %s — skipping", public_id)
        return

    if _too_soon_to_nudge(deal):
        logger.info(
            "[%s] follow_up %s: too soon to nudge — re-enqueuing",
            session.campaign,
            public_id,
        )
        enqueue_follow_up(campaign_id, public_id, delay_seconds=24 * 3600)
        return

    # Check if we've reached the max unanswered follow-ups limit
    if deal.unanswered_follow_up_count >= MAX_UNANSWERED_FOLLOW_UPS:
        logger.info(
            "[%s] follow_up %s: reached max unanswered follow-ups (%d) — marking as unresponsive",
            session.campaign,
            public_id,
            MAX_UNANSWERED_FOLLOW_UPS,
        )
        set_profile_state(
            session, public_id, ProfileState.COMPLETED.value, outcome="unresponsive"
        )
        return

    materialize_profile_summary_if_missing(deal, session)

    # Sync conversation before checking for manual messages, so we have
    # fresh ChatMessage data including any manual messages the user sent
    # directly on LinkedIn.
    from linkedin.db.chat import sync_conversation
    sync_conversation(session, public_id)

    # Conservative pause: check for manual messages (timestamp mismatch)
    if _has_manual_messages_recently(deal, session):
        logger.info(
            "[%s] follow_up %s: manual message detected — pausing indefinitely",
            session.campaign,
            public_id,
        )
        _notify_manual_intervention(session, deal, public_id)
        # Don't re-enqueue - requires manual resume
        return

    decision = run_follow_up_agent(session, deal)

    profile = _build_send_profile(deal)

    if decision.action == "send_message":
        logger.info(
            "[%s] follow_up message for %s: %s",
            session.campaign,
            public_id,
            decision.message,
        )
        sent = send_raw_message(session, profile, decision.message, source="ai")
        if not sent:
            set_profile_state(session, public_id, ProfileState.QUALIFIED.value)
            logger.warning(
                "follow_up for %s: send failed — moving to QUALIFIED for re-connection",
                public_id,
            )
            return
        session.linkedin_profile.record_action(
            ActionLog.ActionType.FOLLOW_UP,
            session.campaign,
        )
        # Increment unanswered follow-up counter
        deal.unanswered_follow_up_count += 1
        deal.save(update_fields=["unanswered_follow_up_count"])
        enqueue_follow_up(
            campaign_id, public_id, delay_seconds=decision.follow_up_hours * 3600
        )

    elif decision.action == "mark_completed":
        set_profile_state(
            session, public_id, ProfileState.COMPLETED.value, outcome=decision.outcome
        )
        logger.info(
            "[%s] follow_up completed for %s: outcome=%s",
            session.campaign,
            public_id,
            decision.outcome,
        )

    elif decision.action == "wait":
        enqueue_follow_up(
            campaign_id, public_id, delay_seconds=decision.follow_up_hours * 3600
        )
