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
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

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


def _has_manual_messages_recently(deal) -> bool:
    """Check if there are any manual outgoing messages in this conversation."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone
    from datetime import timedelta

    ct = ContentType.objects.get_for_model(type(deal.lead))
    
    # Check for any manual outgoing messages in the last 24 hours
    recent_manual = ChatMessage.objects.filter(
        content_type=ct,
        object_id=deal.lead_id,
        source="manual",
        is_outgoing=True,
        creation_date__gte=timezone.now() - timedelta(hours=24)
    ).exists()
    
    return recent_manual


def _notify_manual_intervention(session, deal, public_id: str) -> None:
    """Send notification when manual intervention is detected."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone
    from datetime import timedelta
    
    try:
        ct = ContentType.objects.get_for_model(type(deal.lead))
        
        # Get the most recent manual message
        recent_manual = ChatMessage.objects.filter(
            content_type=ct,
            object_id=deal.lead_id,
            source="manual",
            is_outgoing=True,
            creation_date__gte=timezone.now() - timedelta(hours=24)
        ).order_by('-creation_date').first()
        
        if recent_manual:
            lead_name = deal.lead.public_identifier
            message_preview = recent_manual.content[:100] + "..." if len(recent_manual.content) > 100 else recent_manual.content
            
            # Log notification
            logger.warning(
                "🤖 AI PAUSED: Manual message detected in conversation with %s\n"
                "Message: \"%s\"\n"
                "Campaign: %s\n"
                "To resume AI follow-ups, use Django Admin or run: python manage.py resume_follow_up %s %s",
                lead_name, message_preview, session.campaign.name, 
                session.campaign.pk, lead_name
            )
            
            # TODO: Add email/slack notifications here if needed
            # _send_notification_email(session, deal, recent_manual)
            
    except Exception as e:
        logger.error("Failed to send manual intervention notification for %s → %s", public_id, e)


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
        session.campaign, colored("\u25b6 follow_up", "green", attrs=["bold"]), public_id,
    )

    # Rate limit check
    if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        enqueue_follow_up(campaign_id, public_id, delay_seconds=3600)
        return

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign)
        .select_related("lead", "campaign")
        .first()
    )
    if deal is None:
        logger.warning("follow_up: no Deal for %s — skipping", public_id)
        return

    if _too_soon_to_nudge(deal):
        logger.info("[%s] follow_up %s: too soon to nudge — re-enqueuing", session.campaign, public_id)
        enqueue_follow_up(campaign_id, public_id, delay_seconds=24 * 3600)
        return

    # Conservative pause: check for manual messages
    if _has_manual_messages_recently(deal):
        logger.info("[%s] follow_up %s: manual message detected — pausing indefinitely", session.campaign, public_id)
        _notify_manual_intervention(session, deal, public_id)
        # Don't re-enqueue - requires manual resume
        return

    # Check if we've reached the max unanswered follow-ups limit
    if deal.unanswered_follow_up_count >= MAX_UNANSWERED_FOLLOW_UPS:
        logger.info(
            "[%s] follow_up %s: reached max unanswered follow-ups (%d) — marking as unresponsive",
            session.campaign, public_id, MAX_UNANSWERED_FOLLOW_UPS
        )
        set_profile_state(session, public_id, ProfileState.COMPLETED.value, outcome="unresponsive")
        return

    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)

    profile = _build_send_profile(deal)

    if decision.action == "send_message":
        logger.info("[%s] follow_up message for %s: %s", session.campaign, public_id, decision.message)
        sent = send_raw_message(session, profile, decision.message, source="ai")
        if not sent:
            set_profile_state(session, public_id, ProfileState.QUALIFIED.value)
            logger.warning("follow_up for %s: send failed — moving to QUALIFIED for re-connection", public_id)
            return
        session.linkedin_profile.record_action(
            ActionLog.ActionType.FOLLOW_UP, session.campaign,
        )
        # Increment unanswered follow-up counter
        deal.unanswered_follow_up_count += 1
        deal.save(update_fields=["unanswered_follow_up_count"])
        enqueue_follow_up(campaign_id, public_id, delay_seconds=decision.follow_up_hours * 3600)

    elif decision.action == "mark_completed":
        set_profile_state(session, public_id, ProfileState.COMPLETED.value, outcome=decision.outcome)
        logger.info("[%s] follow_up completed for %s: outcome=%s", session.campaign, public_id, decision.outcome)

    elif decision.action == "wait":
        enqueue_follow_up(campaign_id, public_id, delay_seconds=decision.follow_up_hours * 3600)
