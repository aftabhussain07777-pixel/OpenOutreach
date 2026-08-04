# linkedin/tasks/check_messages.py
"""Unified check-messages task — scans the LinkedIn inbox via Voyager API,
handles BOTH lead replies and proactive follow-up nudges.

Replaces the old follow_up.py and check_messages.py split. The daemon
creates ``CHECK_MESSAGES_SLOTS_PER_DAY`` slots per campaign per day; each
slot scans the inbox and handles everything.

Lazy: the task payload carries only ``campaign_id``. The handler:
1. Calls the Voyager conversations API to list recent conversations.
2. Filters to those whose participant URN matches a tracked lead.
3. For unread conversations → syncs + replies (lead reply path).
4. For read conversations → sends a nudge if ``next_follow_up_at`` is due.
5. For CONNECTED deals not found in the inbox (new connections with
   zero messages) → sends the first outreach if ``next_follow_up_at`` is due.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone
from termcolor import colored

from chat.models import ChatMessage
from linkedin.enums import ProfileState
from linkedin.notification import FailureEvent, notify_failure, notify_opportunity
from linkedin.tasks.follow_up import (
    MAX_UNANSWERED_FOLLOW_UPS,
    MIN_DAYS_PER_UNANSWERED,
    MIN_FOLLOW_UP_HOURS,
    _build_send_profile,
    _has_manual_messages_recently,
    _notify_manual_intervention,
)

logger = logging.getLogger(__name__)


# ── New-message detection ──────────────────────────────────────────────


def _conversation_has_new_activity(
    conv: dict,
    last_sync_at,
    self_urn: str | None = None,
) -> bool | None:
    """Check if a conversation element has new message activity.

    Inspects the conversation element for unread indicators from the Voyager
    API response. Returns ``True`` when new activity is detected, ``False``
    when the conversation is clearly read, and ``None`` when neither signal
    is available (caller should fall back to syncing anyway).

    Checks (in order):
    1. ``unreadCount`` — if > 0, definitely unread.
    2. ``read`` — boolean, ``False`` means unread.
    3. ``lastActivityAt`` — timestamp (ms) newer than *last_sync_at*.
    4. ``messages.elements[0].sender.hostIdentityUrn`` — last message
       sender is NOT the authenticated user (relies on *self_urn*).
    """
    # Check 1: explicit unread count
    unread = conv.get("unreadCount")
    if unread is not None:
        return bool(unread > 0)

    # Check 2: read boolean
    read = conv.get("read")
    if read is not None:
        return not read

    # Check 3: lastActivityAt vs last sync time
    last_activity = conv.get("lastActivityAt")
    if last_activity is not None and last_sync_at is not None:
        import datetime
        activity_dt = datetime.datetime.fromtimestamp(
            last_activity / 1000, tz=datetime.timezone.utc,
        )
        if activity_dt > last_sync_at:
            return _last_message_from_lead(conv, self_urn, activity_dt, last_sync_at)

    # No signal available — caller should sync to be safe
    return None


def _last_message_from_lead(
    conv: dict,
    self_urn: str | None,
    activity_dt: object,
    last_sync_at,
) -> bool:
    """Check if the last message in the conversation was sent by a lead."""
    if not self_urn:
        return True
    msg_elements = (
        conv.get("messages", {})
        .get("elements", [])
    )
    for msg in msg_elements:
        sender_urn = (
            msg.get("sender", {})
            .get("hostIdentityUrn") or
            msg.get("actor", {})
            .get("hostIdentityUrn", "")
        )
        if sender_urn and sender_urn != self_urn:
            return True
        if sender_urn == self_urn:
            return False
    return True


# ── Follow-up scheduling helpers ───────────────────────────────────────


def _lead_has_replied(deal) -> bool:
    """Check if the lead has ever sent an incoming message."""
    from django.contrib.contenttypes.models import ContentType
    return ChatMessage.objects.filter(
        content_type=ContentType.objects.get_for_model(type(deal.lead)),
        object_id=deal.lead_id,
        is_outgoing=False,
    ).exists()


def _stamp_next_follow_up(deal, delay_hours: float) -> None:
    """Set `deal.next_follow_up_at` to ``now + delay_hours``, scaling the
    minimum delay with unanswered follow-up count so each consecutive nudge
    waits ``MIN_DAYS_PER_UNANSWERED * unanswered_count`` days minimum."""
    # Scale the floor: each unanswered nudge adds days, not hours
    min_hours = max(
        MIN_FOLLOW_UP_HOURS,
        deal.unanswered_follow_up_count * MIN_DAYS_PER_UNANSWERED * 24,
    )
    delay = max(delay_hours, min_hours)
    deal.next_follow_up_at = timezone.now() + timedelta(hours=delay)
    deal.save(update_fields=["next_follow_up_at"])
    logger.debug(
        "stamp_next_follow_up for %s: delay=%dh (LLM=%dh, min=%dh, unanswered=%d)",
        deal.lead.public_identifier, delay, delay_hours, min_hours,
        deal.unanswered_follow_up_count,
    )


def _is_follow_up_due(deal) -> bool:
    """Check if this deal is ready for a nudge/first-message."""
    if deal.next_follow_up_at is None:
        # Freshly connected, no follow-up scheduled yet — due immediately.
        return True
    return timezone.now() >= deal.next_follow_up_at


# ── Nudge handler ──────────────────────────────────────────────────────


def _handle_follow_up_nudge(session, deal, conv=None):
    """Send a proactive follow-up nudge (or first message for new connections).

    Handles:
    - Max-unanswered limit → complete as unresponsive.
    - Manual message detection → stamp 7d delay and skip.
    - LLM-agent decision → send nudge / mark completed / wait.
    """
    from linkedin.actions.message import send_raw_message
    from linkedin.agents.follow_up import run_follow_up_agent
    from linkedin.db.chat import sync_conversation
    from linkedin.db.deals import set_profile_state
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.models import ActionLog

    campaign = session.campaign
    public_id = deal.lead.public_identifier

    if not session.linkedin_profile.can_execute(ActionLog.ActionType.FOLLOW_UP):
        logger.info(
            "[%s] check_messages nudge %s: daily follow-up limit reached — skipping",
            campaign, public_id,
        )
        return

    # Max unanswered check
    if deal.unanswered_follow_up_count >= MAX_UNANSWERED_FOLLOW_UPS:
        logger.info(
            "[%s] check_messages nudge %s: reached max unanswered (%d) — "
            "marking as unresponsive",
            campaign, public_id, MAX_UNANSWERED_FOLLOW_UPS,
        )
        set_profile_state(
            session, public_id, ProfileState.COMPLETED.value, outcome="unresponsive",
        )
        return

    materialize_profile_summary_if_missing(deal, session)

    # Sync so we have fresh data for manual-message detection
    sync_conversation(session, public_id)

    # Manual-message detection
    if _has_manual_messages_recently(deal, session):
        logger.info(
            "[%s] check_messages nudge %s: manual message detected — skipping 7d",
            campaign, public_id,
        )
        _notify_manual_intervention(session, deal, public_id)
        deal.unanswered_follow_up_count += 1
        if deal.unanswered_follow_up_count >= MAX_UNANSWERED_FOLLOW_UPS:
            set_profile_state(
                session, public_id, ProfileState.COMPLETED.value,
                outcome="unresponsive",
            )
            return
        deal.save(update_fields=["unanswered_follow_up_count"])
        _stamp_next_follow_up(deal, delay_hours=7 * 24)  # 7 days
        return

    decision = run_follow_up_agent(session, deal)

    # Persist agent decision fields on the deal for logging / analysis
    deal.agent_user_states = decision.user_states.model_dump()
    deal.agent_objective_category = decision.objective.category
    deal.agent_objective = decision.objective.description
    deal.agent_action_reason = decision.action_reason
    deal.agent_conversation_summary = decision.conversation_summary
    deal.save(update_fields=[
        "agent_user_states", "agent_objective_category",
        "agent_objective", "agent_action_reason",
        "agent_conversation_summary",
    ])

    profile = _build_send_profile(deal)

    if decision.action == "send_message":
        logger.info(
            "[%s] check_messages nudge %s: %s", campaign, public_id, decision.message,
        )
        sent = send_raw_message(session, profile, decision.message, source="ai")
        if not sent:
            logger.warning(
                "check_messages nudge %s: send failed — moving to QUALIFIED",
                public_id,
            )
            notify_failure(FailureEvent(
                title="Follow-up Nudge Send Failed",
                detail=f"Nudge to {public_id} failed — deal moved to QUALIFIED",
                campaign=str(campaign),
                task_type="check_messages",
                lead=public_id,
            ))
            set_profile_state(session, public_id, ProfileState.QUALIFIED.value)
            return

        session.linkedin_profile.record_action(
            ActionLog.ActionType.FOLLOW_UP, session.campaign,
        )
        deal.unanswered_follow_up_count += 1
        deal.save(update_fields=["unanswered_follow_up_count"])

        delay_hours = max(decision.follow_up_hours, MIN_FOLLOW_UP_HOURS)
        _stamp_next_follow_up(deal, delay_hours=delay_hours)

        # Sync to persist the outgoing message locally
        try:
            sync_conversation(session, public_id)
        except Exception:
            logger.exception(
                "check_messages nudge: post-send sync failed for %s (best-effort)",
                public_id,
            )
            notify_failure(FailureEvent(
                title="Post-Nudge Sync Failed",
                detail=f"Conversation sync after nudge failed for {public_id}",
                campaign=str(campaign),
                task_type="check_messages",
                lead=public_id,
            ))
        deal.save()  # bump update_date

    elif decision.action == "mark_completed":
        set_profile_state(
            session, public_id,
            ProfileState.COMPLETED.value, outcome=decision.outcome,
        )
        logger.info(
            "[%s] check_messages nudge completed %s → %s",
            campaign, public_id, decision.outcome,
        )

    elif decision.action == "wait":
        # Bump update_date so we don't keep trying this deal on every scan
        deal.save()


# ── Main handler ───────────────────────────────────────────────────────


def handle_check_messages(task, session, qualifiers):
    """Scan the LinkedIn inbox for tracked leads, then handle both replies
    and proactive nudges. Once a lead has replied, all follow-up activity
    stops for that deal (temporary guard)."""
    from crm.models import Deal, Lead
    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.api.messaging import fetch_conversations
    from linkedin.db.chat import sync_conversation
    from linkedin.db.deals import set_profile_state
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.models import ActionLog

    campaign = session.campaign

    # ── 1. Gather tracked CONNECTED deals ───────────────────────────
    tracked_deals = list(
        Deal.objects.filter(
            campaign=campaign,
            state=ProfileState.CONNECTED,
            outcome="",
            lead__disqualified=False,
            lead__urn__isnull=False,
        ).select_related("lead")
    )

    if not tracked_deals:
        logger.info("[%s] check_messages: no CONNECTED leads with URNs", campaign)
        return

    tracked_urns: dict[str, Deal] = {}
    urn_to_last_sync: dict[str, object] = {}
    ct = ContentType.objects.get_for_model(Lead)

    for deal in tracked_deals:
        urn = deal.lead.urn
        tracked_urns[urn] = deal

        last_msg = (
            ChatMessage.objects.filter(
                content_type=ct, object_id=deal.lead_id,
            )
            .order_by("-creation_date")
            .values_list("creation_date", flat=True)
            .first()
        )
        urn_to_last_sync[urn] = last_msg

    # ── 2. Fetch inbox conversations ────────────────────────────────
    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)
    mailbox_urn = session.self_profile["urn"]

    raw = fetch_conversations(api, mailbox_urn)
    elements = (
        raw.get("data", {})
        .get("messengerConversationsBySyncToken", {})
        .get("elements", [])
    )

    # ── 3. Process inbox conversations ──────────────────────────────
    seen_urns: set[str] = set()
    replied_count = 0
    nudged_count = 0

    for conv in elements:
        # Match tracked lead
        matched_urn = None
        for p in conv.get("conversationParticipants", []):
            host_urn = p.get("hostIdentityUrn")
            if host_urn in tracked_urns:
                matched_urn = host_urn
                break

        if matched_urn is None:
            continue  # unknown user — skip (inbound inquiry)

        seen_urns.add(matched_urn)
        deal = tracked_urns[matched_urn]
        public_id = deal.lead.public_identifier

        # TEMPORARY: once a lead has replied, stop ALL follow-up activity
        if _lead_has_replied(deal):
            logger.debug(
                "[%s] check_messages %s: lead already replied — skipping (temp guard)",
                campaign, public_id,
            )
            continue

        has_new = _conversation_has_new_activity(
            conv, urn_to_last_sync.get(matched_urn), self_urn=mailbox_urn,
        )

        if has_new is False:
            # Conversation is read — no new message from lead
            # Check if it's time for a proactive nudge
            if _is_follow_up_due(deal):
                logger.info(
                    "[%s] %s %s — nudge due (next_follow_up_at due)",
                    campaign,
                    colored("\u25b6 check_messages nudge", "green", attrs=["bold"]),
                    public_id,
                )
                _handle_follow_up_nudge(session, deal, conv=conv)
                nudged_count += 1
            continue

        if has_new is None:
            # No signal — sync to be safe
            logger.debug(
                "check_messages: no unread signal for %s — falling back to sync",
                matched_urn,
            )

        # ── LEAD REPLIED path ───────────────────────────────────────
        sync_conversation(session, public_id)

        # Check for new incoming messages since last outgoing
        last_outgoing = (
            ChatMessage.objects.filter(
                content_type=ct, object_id=deal.lead_id, is_outgoing=True,
            )
            .order_by("-creation_date")
            .first()
        )

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
            logger.debug(
                "[%s] check_messages %s: synced but no new incoming messages",
                campaign, public_id,
            )
            # Still check nudge eligibility
            if _is_follow_up_due(deal):
                logger.info(
                    "[%s] %s %s — nudge due (no lead reply after sync)",
                    campaign,
                    colored("\u25b6 check_messages nudge", "green", attrs=["bold"]),
                    public_id,
                )
                _handle_follow_up_nudge(session, deal, conv=conv)
                nudged_count += 1
            continue

        logger.info(
            "[%s] %s %s \u2014 new message from lead: %.80s",
            campaign,
            colored("\u25b6 check_messages", "yellow", attrs=["bold"]),
            public_id,
            new_incoming.content or "",
        )

        # Manual intervention check
        if _has_manual_messages_recently(deal, session):
            logger.info(
                "[%s] check_messages %s: manual message detected \u2014 skipping reply",
                campaign, public_id,
            )
            _notify_manual_intervention(session, deal, public_id)
            deal.save()
            _stamp_next_follow_up(deal, delay_hours=7 * 24)  # 7d backoff
            continue

        # Generate reply
        from linkedin.actions.message import send_raw_message
        from linkedin.agents.follow_up import run_follow_up_agent
        from linkedin.agents.opportunity_detector import run_opportunity_detector, RecentMessage

        materialize_profile_summary_if_missing(deal, session)
        decision = run_follow_up_agent(session, deal)

        # Persist agent decision fields for logging / analysis
        deal.agent_user_states = decision.user_states.model_dump()
        deal.agent_objective_category = decision.objective.category
        deal.agent_objective = decision.objective.description
        deal.agent_action_reason = decision.action_reason
        deal.agent_conversation_summary = decision.conversation_summary
        deal.save(update_fields=[
            "agent_user_states", "agent_objective_category",
            "agent_objective", "agent_action_reason",
            "agent_conversation_summary",
        ])

        # Capture latest_reply content before processing the decision
        latest_reply_content = new_incoming.content or ""

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
                notify_failure(FailureEvent(
                    title="Message Send Failed",
                    detail=f"Reply to {public_id} failed — deal moved to QUALIFIED",
                    campaign=str(campaign),
                    task_type="check_messages",
                    lead=public_id,
                ))
                set_profile_state(session, public_id, ProfileState.QUALIFIED.value)
                continue

            session.linkedin_profile.record_action(
                ActionLog.ActionType.FOLLOW_UP, session.campaign,
            )
            replied_count += 1

            if deal.unanswered_follow_up_count > 0:
                deal.unanswered_follow_up_count = 0
                deal.save(update_fields=["unanswered_follow_up_count"])

            delay_hours = max(decision.follow_up_hours, MIN_FOLLOW_UP_HOURS)
            _stamp_next_follow_up(deal, delay_hours=delay_hours)

            try:
                sync_conversation(session, public_id)
            except Exception:
                logger.exception(
                    "check_messages: post-reply sync failed for %s (best-effort)",
                    public_id,
                )
                notify_failure(FailureEvent(
                    title="Post-Reply Sync Failed",
                    detail=f"Conversation sync after reply failed for {public_id}",
                    campaign=str(campaign),
                    task_type="check_messages",
                    lead=public_id,
                ))

            # ── Opportunity Detector ────────────────────────────────────
            # Run on every successfully sent reply to assess business potential
            try:
                recent_qs = (
                    ChatMessage.objects.filter(
                        content_type=ct, object_id=deal.lead_id,
                    )
                    .order_by("-creation_date", "-pk")[:10]
                )
                recent_msgs = [
                    RecentMessage(
                        content=m.content or "",
                        is_outgoing=m.is_outgoing,
                        timestamp=m.creation_date,
                    )
                    for m in reversed(list(recent_qs))
                ]

                assessment = run_opportunity_detector(
                    session, deal,
                    latest_reply=latest_reply_content,
                    recent_messages=recent_msgs,
                )

                deal.opportunity_assessment = assessment.model_dump()
                deal.save(update_fields=["opportunity_assessment"])

                from linkedin.models import SiteConfig
                config = SiteConfig.load()
                threshold = config.opportunity_score_threshold

                if assessment.notify_recommended or assessment.opportunity_score >= threshold:
                    notify_opportunity(
                        campaign=str(campaign),
                        lead=public_id,
                        opportunity_score=assessment.opportunity_score,
                        threshold=threshold,
                        notify_recommended=assessment.notify_recommended,
                        opportunity_type=assessment.opportunity_type,
                        summary=assessment.summary,
                        evidence=assessment.evidence,
                    )
                    logger.info(
                        "[%s] opportunity notification sent for %s "
                        "(score=%.2f threshold=%.2f notify=%s)",
                        campaign, public_id,
                        assessment.opportunity_score, threshold,
                        assessment.notify_recommended,
                    )
            except Exception:
                logger.exception(
                    "[%s] opportunity detector failed for %s (best-effort)",
                    campaign, public_id,
                )

        elif decision.action == "mark_completed":
            set_profile_state(
                session, public_id,
                ProfileState.COMPLETED.value, outcome=decision.outcome,
            )
            logger.info(
                "[%s] check_messages: completed %s \u2192 %s",
                campaign, public_id, decision.outcome,
            )

    # ── 4. Orphan deals (not in inbox scan) ──────────────────────────
    # Deals whose conversations weren't in the first page of API results
    # (pagination) or whose URN wasn't resolved yet. Differentiate between
    # inbox-misses (have existing messages) and true new connections (zero
    # messages) for correct logging.
    for deal in tracked_deals:
        if deal.lead.urn in seen_urns:
            continue  # already handled above

        public_id = deal.lead.public_identifier
        if not _is_follow_up_due(deal):
            continue

        # TEMPORARY: once a lead has replied, stop ALL follow-up activity
        if _lead_has_replied(deal):
            logger.debug(
                "[%s] check_messages %s: lead already replied — skipping orphan (temp guard)",
                campaign, public_id,
            )
            continue

        # Check if this deal has any existing conversation history
        has_messages = ChatMessage.objects.filter(
            content_type=ct, object_id=deal.lead_id,
        ).exists()

        if has_messages:
            logger.info(
                "[%s] %s %s — nudge due (conversation not in recent inbox)",
                campaign,
                colored("\u25b6 check_messages nudge", "green", attrs=["bold"]),
                public_id,
            )
        else:
            logger.info(
                "[%s] %s %s — first outreach due (new connection, no messages yet)",
                campaign,
                colored("\u25b6 check_messages first", "cyan", attrs=["bold"]),
                public_id,
            )

        _handle_follow_up_nudge(session, deal)
        nudged_count += 1

    if replied_count:
        logger.info(
            "[%s] check_messages: replied to %d new message(s)",
            campaign, replied_count,
        )
    if nudged_count:
        logger.info(
            "[%s] check_messages: sent %d follow-up nudge(s)",
            campaign, nudged_count,
        )
    if not replied_count and not nudged_count:
        logger.info("[%s] check_messages: no new activity found", campaign)
