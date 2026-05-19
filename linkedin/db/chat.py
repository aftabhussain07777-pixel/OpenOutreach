import logging

logger = logging.getLogger(__name__)


def _get_lead_and_ct(public_identifier: str):
    """Return (lead, content_type) for a public identifier."""
    from django.contrib.contenttypes.models import ContentType

    from crm.models import Lead

    lead = Lead.objects.get(public_identifier=public_identifier)
    ct = ContentType.objects.get_for_model(lead)
    return lead, ct


def sync_conversation(session, public_identifier: str) -> list[dict]:
    """Fetch messages from Voyager API and upsert into ChatMessage.

    Returns messages as a list of {sender, text, timestamp, is_outgoing} dicts
    from the DB (always the source of truth after sync). Newly-synced messages
    are also folded into the campaign Deal's `chat_summary` (mem0-style facts).
    """
    lead, ct = _get_lead_and_ct(public_identifier)
    new_messages = _sync_from_api(session, public_identifier, lead, ct)
    _update_deal_chat_summary(session, lead, new_messages)

    return _read_from_db(public_identifier)


def _update_deal_chat_summary(session, lead, new_messages):
    """Fold newly-synced ChatMessages into the campaign Deal's `chat_summary`."""
    if not new_messages:
        return
    from crm.models import Deal
    from linkedin.db.summaries import update_chat_summary

    deal = Deal.objects.filter(lead=lead, campaign=session.campaign).first()
    if not deal:
        return
    update_chat_summary(deal, new_messages)

    # Reset unanswered counter if lead replied
    _reset_unanswered_counter_if_lead_replied(deal, new_messages)


def _sync_from_api(session, public_identifier: str, lead, ct) -> list:
    """Fetch messages from Voyager API and upsert into DB.

    Returns the list of newly-created ``ChatMessage`` rows (in arrival order),
    so callers can incrementally update derived caches like ``chat_summary``.
    """
    from chat.models import ChatMessage
    from linkedin.actions.conversations import (
        find_conversation_urn,
        find_conversation_urn_via_navigation,
        parse_message_element,
    )
    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.api.messaging import fetch_messages

    session.ensure_browser()
    api = PlaywrightLinkedinAPI(session=session)

    target_urn = lead.get_urn(session)
    mailbox_urn = session.self_profile["urn"]

    # Find conversation URN
    conversation_urn = find_conversation_urn(api, target_urn, mailbox_urn)
    if not conversation_urn:
        conversation_urn = find_conversation_urn_via_navigation(session, target_urn)
    if not conversation_urn:
        logger.debug("sync: no conversation found for %s", public_identifier)
        return []

    # Fetch messages
    raw = fetch_messages(api, conversation_urn)
    elements = (
        raw.get("data", {}).get("messengerMessagesBySyncToken", {}).get("elements", [])
    )

    self_urn = session.self_profile["urn"]
    new_messages: list = []

    for msg in elements:
        parsed = parse_message_element(msg)
        if not parsed or not parsed["entityUrn"]:
            continue

        is_outgoing = parsed["sender_host_urn"] == self_urn

        # Determine message source
        source = "manual"  # default to manual
        if is_outgoing:
            # Check if this is an AI message by looking for recent ActionLog.FOLLOW_UP
            source = _detect_message_source(session, lead, parsed["delivered_at"])

        # Upsert by linkedin_urn
        obj, created = ChatMessage.objects.update_or_create(
            linkedin_urn=parsed["entityUrn"],
            defaults={
                "content_type": ct,
                "object_id": lead.pk,
                "content": parsed["text"],
                "is_outgoing": is_outgoing,
                "source": source,
                "owner": session.django_user,
                **(
                    {"creation_date": parsed["delivered_at"]}
                    if parsed["delivered_at"]
                    else {}
                ),
            },
        )
        if created:
            new_messages.append(obj)
            logger.debug(
                "sync: new message from %s for %s",
                parsed["sender_name"],
                public_identifier,
            )

    # Sort new messages chronologically so the LLM sees them in order.
    new_messages.sort(key=lambda m: m.creation_date or m.pk)
    logger.debug(
        "sync: processed %d messages for %s (%d new)",
        len(elements),
        public_identifier,
        len(new_messages),
    )
    return new_messages


def _read_from_db(public_identifier: str) -> list[dict]:
    """Read all ChatMessages for a lead, sorted chronologically."""
    from chat.models import ChatMessage

    lead, ct = _get_lead_and_ct(public_identifier)
    lead_name = lead.public_identifier or "them"

    messages = (
        ChatMessage.objects.filter(
            content_type=ct,
            object_id=lead.pk,
        )
        .select_related("owner")
        .order_by("creation_date")
    )

    result = []
    for msg in messages:
        if not msg.content:
            continue
        if msg.is_outgoing:
            owner = msg.owner
            sender = (
                f"{owner.first_name or ''} {owner.last_name or ''}".strip()
                if owner
                else "me"
            )
        else:
            sender = lead_name
        result.append(
            {
                "sender": sender or "me",
                "text": msg.content,
                "timestamp": msg.creation_date.strftime("%Y-%m-%d %H:%M")
                if msg.creation_date
                else "",
                "is_outgoing": msg.is_outgoing,
            }
        )
    return result


def _detect_message_source(session, lead, delivered_at) -> str:
    """Detect if a message was sent by AI vs manual by checking recent ActionLogs.

    Uses a tight ±2-minute window around ``delivered_at`` and scopes the
    ActionLog lookup to the current campaign so cross-campaign follow-ups
    never cause false AI classifications.

    This keeps ``source`` accurate for analytics / admin display but is NOT
    used for manual-intervention detection — that is done via timestamp
    comparison in :func:`~linkedin.tasks.follow_up._has_manual_messages_recently`.
    """
    from datetime import timedelta

    from linkedin.models import ActionLog

    if not delivered_at:
        return "manual"

    try:
        window = timedelta(minutes=2)
        recent_time = delivered_at - window
        future_time = delivered_at + window

        action_log = ActionLog.objects.filter(
            linkedin_profile=session.linkedin_profile,
            campaign=session.campaign,
            action_type=ActionLog.ActionType.FOLLOW_UP,
            created_at__gte=recent_time,
            created_at__lte=future_time,
        ).first()

        if action_log:
            logger.debug(
                "_detect_message_source: %s → ai (ActionLog at %s within ±2m of %s)",
                lead.public_identifier,
                action_log.created_at,
                delivered_at,
            )
            return "ai"
        return "manual"
    except Exception as e:
        logger.debug(
            "Failed to detect message source for %s → %s", lead.public_identifier, e
        )
        return "manual"


def _reset_unanswered_counter_if_lead_replied(deal, new_messages) -> None:
    """Reset unanswered follow-up counter if lead sent a message."""
    # Check if any new message is incoming (from lead)
    has_incoming = any(not msg.is_outgoing for msg in new_messages)

    if has_incoming and deal.unanswered_follow_up_count > 0:
        deal.unanswered_follow_up_count = 0
        deal.save(update_fields=["unanswered_follow_up_count"])
        logger.debug(
            "Reset unanswered_follow_up_count for %s (lead replied)",
            deal.lead.public_identifier,
        )
