# linkedin/tasks/check_messages.py
"""Check messages task — scans all CONNECTED deals for new incoming
messages and triggers a follow-up reply when one is found.

Lazy: the task payload carries only ``campaign_id``. The handler resolves
every CONNECTED deal with an existing conversation, syncs the latest
messages, and replies to any new incoming messages from the lead.
"""

from __future__ import annotations

import logging

from django.contrib.contenttypes.models import ContentType
from termcolor import colored

from chat.models import ChatMessage
from linkedin.enums import ProfileState
from linkedin.tasks.follow_up import MIN_FOLLOW_UP_HOURS, _has_manual_messages_recently, _notify_manual_intervention

logger = logging.getLogger(__name__)


def handle_check_messages(task, session, qualifiers):
    """Scan all CONNECTED deals for new incoming messages and reply.
    
    Runs once per day per campaign (planned by ``plan_check_messages_window``).
    Synces each conversation, detects incoming messages not yet replied to,
    and uses the follow-up agent to generate an appropriate reply.
    """
    from crm.models import Deal
    from linkedin.actions.message import send_raw_message
    from linkedin.agents.follow_up import run_follow_up_agent
    from linkedin.db.chat import sync_conversation
    from linkedin.db.deals import set_profile_state
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.models import ActionLog
    from linkedin.tasks.scheduler import enqueue_follow_up

    campaign = session.campaign

    # Find all CONNECTED deals (active conversations)
    deals = (
        Deal.objects.filter(
            campaign=campaign,
            state=ProfileState.CONNECTED,
            outcome="",
            lead__disqualified=False,
        )
        .select_related("lead", "campaign")
        .order_by("update_date")
    )

    replied_count = 0
    for deal in deals:
        public_id = deal.lead.public_identifier

        # Sync conversation to get latest messages from LinkedIn
        sync_conversation(session, public_id)

        # Check for new incoming messages since the last AI-sent reply
        ct = ContentType.objects.get_for_model(type(deal.lead))

        last_outgoing = (
            ChatMessage.objects.filter(
                content_type=ct,
                object_id=deal.lead_id,
                is_outgoing=True,
            )
            .order_by("-creation_date")
            .first()
        )

        # Look for incoming messages after the latest outgoing message
        incoming_filter = {
            "content_type": ct,
            "object_id": deal.lead_id,
            "is_outgoing": False,
        }
        if last_outgoing:
            incoming_filter["creation_date__gt"] = last_outgoing.creation_date

        new_incoming = (
            ChatMessage.objects.filter(**incoming_filter)
            .order_by("creation_date")
            .first()
        )
        if not new_incoming:
            continue  # no new messages from this lead

        logger.info(
            "[%s] %s %s — new message from lead: %.80s",
            campaign,
            colored("\u25b6 check_messages", "yellow", attrs=["bold"]),
            public_id,
            new_incoming.content or "",
        )

        # Check for manual messages — if a human typed something recently,
        # back off the AI from this conversation.
        if _has_manual_messages_recently(deal, session):
            logger.info(
                "[%s] check_messages %s: manual message detected — skipping reply",
                session.campaign,
                public_id,
            )
            _notify_manual_intervention(session, deal, public_id)
            deal.save()  # bump update_date
            continue

        # Generate a reply using the follow-up agent
        materialize_profile_summary_if_missing(deal, session)
        decision = run_follow_up_agent(session, deal)

        if decision.action == "send_message":
            profile = {
                "public_identifier": public_id,
                "urn": deal.lead.urn or "",
            }
            sent = send_raw_message(session, profile, decision.message, source="ai")
            if not sent:
                logger.warning(
                    "check_messages: reply to %s failed — moving to QUALIFIED",
                    public_id,
                )
                set_profile_state(session, public_id, ProfileState.QUALIFIED.value)
                continue

            session.linkedin_profile.record_action(
                ActionLog.ActionType.FOLLOW_UP,
                session.campaign,
            )
            replied_count += 1

            # Reset unanswered counter since the lead replied
            if deal.unanswered_follow_up_count > 0:
                deal.unanswered_follow_up_count = 0
                deal.save(update_fields=["unanswered_follow_up_count"])

            # Schedule next follow-up at the LLM-recommended interval
            delay_hours = max(decision.follow_up_hours, MIN_FOLLOW_UP_HOURS)
            enqueue_follow_up(campaign.pk, public_id, delay_seconds=delay_hours * 3600)

            # Sync the message we just sent so chat_summary stays current
            try:
                sync_conversation(session, public_id)
            except Exception:
                logger.exception(
                    "check_messages: post-reply sync failed for %s (best-effort)",
                    public_id,
                )

        elif decision.action == "mark_completed":
            set_profile_state(
                session,
                public_id,
                ProfileState.COMPLETED.value,
                outcome=decision.outcome,
            )
            logger.info(
                "[%s] check_messages: completed %s → %s",
                campaign,
                public_id,
                decision.outcome,
            )

        # "wait" action: no reply needed, skip

    if replied_count:
        logger.info(
            "[%s] check_messages: replied to %d new message(s)",
            campaign,
            replied_count,
        )
    else:
        logger.info("[%s] check_messages: no new messages found", campaign)
