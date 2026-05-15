# linkedin/actions/message.py
import logging
from typing import Any, Dict

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator

from linkedin.browser.nav import dump_page_html, goto_page, human_type

logger = logging.getLogger(__name__)

LINKEDIN_MESSAGING_URL = "https://www.linkedin.com/messaging/thread/new/"

# Selector fallback chains: semantic/ARIA first, then class-based.
# LinkedIn A/B tests UI variants per account and renames classes often.
# Each key maps to a list tried in order; first with a match wins.
SELECTOR_CHAINS = {
    # ── New thread: recipient search ──
    "connections_input": [
        'input[role="combobox"][placeholder*="name"]',
        'input[class*="msg-connections"]',
        'input[placeholder*="Type a name"]',
        'input[type="text"][aria-owns]',
    ],
    "search_result_row": [
        'ul[role="listbox"] li[role="option"]',
        'div[class*="msg-connections-typeahead__search-result-row"]',
        'li[class*="search-result"]',
    ],
    # ── Thread: compose area ──
    "compose_input": [
        'div[role="textbox"][aria-label*="Write a message"]',
        'div[role="textbox"][aria-label*="message"i]',
        'div[class*="msg-form__contenteditable"]',
        'div[contenteditable="true"]',
    ],
    "compose_send": [
        'button[type="submit"][class*="msg-form"]',
        'button[class*="send-btn"]',
        'button[class*="send-button"]',
        'form button[type="submit"]',
        'button[type="submit"]',
    ],
}


def _find(page, key: str, timeout: int = 5000) -> Locator:
    """Try each selector in the chain for *key*, return the first with matches.

    Raises PlaywrightError if none match within *timeout* ms.
    """
    chain = SELECTOR_CHAINS[key]
    for sel in chain:
        loc = page.locator(sel)
        try:
            loc.first.wait_for(state="attached", timeout=timeout)
            logger.debug("Selector hit for %s: %s", key, sel)
            return loc
        except (PlaywrightError, TimeoutError):
            continue
    tried = ", ".join(chain)
    raise PlaywrightError(f"No selector matched for '{key}'. Tried: {tried}")


# ── Public entry point ────────────────────────────────────────────


def send_raw_message(
    session, profile: Dict[str, Any], message: str, source: str = "manual"
) -> bool:
    """Send an arbitrary message to a profile. Returns True if sent."""
    public_identifier = profile.get("public_identifier")

    if _send_message(session, profile, message, source=source):
        return True
    dump_page_html(session, profile, category="message_direct")

    if _send_message_via_api(session, profile, message, source=source):
        return True

    logger.error("All send methods failed for %s", public_identifier)
    return False


def _send_message(
    session, profile: Dict[str, Any], message: str, source: str = "manual"
) -> bool:
    """Navigate to /messaging/thread/new/?recipient=<urn>, compose, send.

    Uses the target URN (promoted to its own Lead column in crm.0005) to
    skip the search-by-name step entirely. Post-migration 0007 the Lead
    row no longer carries first_name/last_name, so name-based search is
    not available anyway.
    """
    from linkedin.api.messaging.utils import encode_urn

    public_identifier = profile.get("public_identifier")
    target_urn = profile.get("urn")
    if not target_urn:
        logger.error("Cannot send via direct thread: no URN for %s", public_identifier)
        return False
    try:
        thread_url = f"{LINKEDIN_MESSAGING_URL}?recipient={encode_urn(target_urn)}"
        goto_page(
            session,
            action=lambda: session.page.goto(thread_url),
            expected_url_pattern="/messaging",
            timeout=30_000,
            error_message="Error opening messaging thread",
        )
        session.wait(1, 2)

        human_type(
            _find(session.page, "compose_input").first,
            message,
            min_delay=10,
            max_delay=50,
        )
        _find(session.page, "compose_send").first.click(delay=200)
        session.wait(0.5, 1)
        logger.info("Message sent to %s (direct thread)", public_identifier)
        if source == "ai":
            _mark_message_as_ai(session, public_identifier, message)
        return True
    except (PlaywrightError, TimeoutError) as e:
        logger.error(
            "Failed to send message to %s (direct thread) → %s", public_identifier, e
        )
        return False


def _send_message_via_api(
    session, profile: Dict[str, Any], message: str, source: str = "manual"
) -> bool:
    """Last-resort fallback: send via Voyager Messaging API.

    Requires profile dict to contain 'urn' (target profile URN).
    """
    from linkedin.actions.conversations import (
        find_conversation_urn,
        find_conversation_urn_via_navigation,
    )
    from linkedin.api.client import PlaywrightLinkedinAPI
    from linkedin.api.messaging import send_message

    public_identifier = profile.get("public_identifier")
    target_urn = profile.get("urn")
    if not target_urn:
        logger.error(
            "API send failed for %s → no URN in profile dict", public_identifier
        )
        return False

    mailbox_urn = session.self_profile["urn"]
    api = PlaywrightLinkedinAPI(session=session)

    conversation_urn = find_conversation_urn(api, target_urn, mailbox_urn)
    if not conversation_urn:
        conversation_urn = find_conversation_urn_via_navigation(session, target_urn)
    if not conversation_urn:
        logger.error(
            "API send failed for %s → no conversation found", public_identifier
        )
        return False

    try:
        send_message(api, conversation_urn, message, mailbox_urn)
        logger.info("Message sent to %s (API fallback)", public_identifier)
        if source == "ai":
            _mark_message_as_ai(session, public_identifier, message)
        return True
    except Exception as e:
        logger.error("API send failed for %s → %s", public_identifier, e)
        return False


def _mark_message_as_ai(session, public_identifier: str, message_content: str) -> None:
    """Mark the most recent outgoing message as AI-sent.

    This is a best-effort fallback that runs immediately after send.  The
    authoritative path is
    :func:`~linkedin.tasks.follow_up._mark_latest_outgoing_as_ai`, which
    runs after a post-send ``sync_conversation`` and doesn't need content
    matching.

    We try content matching first (fast path — the message row may exist
    if sync was called before send in a different code path), then fall
    back to "latest outgoing" which is reliable when sync has run after
    send.
    """
    from datetime import timedelta

    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone

    from chat.models import ChatMessage
    from crm.models import Lead

    try:
        lead = Lead.objects.get(public_identifier=public_identifier)
        ct = ContentType.objects.get_for_model(lead)

        recent_time = timezone.now() - timedelta(minutes=5)

        # Fast path: match by content prefix (works when ChatMessage was
        # already created by a prior sync).
        message = (
            ChatMessage.objects.filter(
                content_type=ct,
                object_id=lead.pk,
                content__icontains=message_content[:50],
                is_outgoing=True,
                creation_date__gte=recent_time,
            )
            .order_by("-creation_date")
            .first()
        )

        # Slow path: content match failed (common when sync runs before
        # send).  Fall back to the most recent outgoing.
        if message is None:
            message = (
                ChatMessage.objects.filter(
                    content_type=ct,
                    object_id=lead.pk,
                    is_outgoing=True,
                    creation_date__gte=recent_time,
                )
                .order_by("-creation_date")
                .first()
            )

        if message and message.source != "ai":
            message.source = "ai"
            message.save(update_fields=["source"])
            logger.debug("Marked message as AI-sent for %s", public_identifier)
        elif message is None:
            logger.debug(
                "_mark_message_as_ai: no recent outgoing ChatMessage for %s "
                "(sync may not have run yet — this is normal; "
                "_mark_latest_outgoing_as_ai in follow_up handler is the "
                "authoritative path)",
                public_identifier,
            )
    except Exception as e:
        logger.error("Failed to mark message as AI for %s → %s", public_identifier, e)


if __name__ == "__main__":
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Debug LinkedIn messaging search results")
    parser.add_argument("--name", required=True, help="Full name to search for")
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    logger.info("Searching for '%s' ...", args.name)

    goto_page(
        session,
        action=lambda: session.page.goto(LINKEDIN_MESSAGING_URL),
        expected_url_pattern="/messaging",
        timeout=30_000,
        error_message="Error opening messaging",
    )

    conn_input = _find(session.page, "connections_input").first
    conn_input.fill("")
    session.wait(0.5, 1)
    human_type(conn_input, args.name, min_delay=10, max_delay=50)
    session.wait(3, 4)

    rows = _find(session.page, "search_result_row")
    count = rows.count()
    logger.info("=== Found %d result rows ===", count)
    for i in range(min(count, 3)):
        row = rows.nth(i)
        logger.debug("--- Row %d inner_text ---", i)
        logger.debug(row.inner_text(timeout=5_000))
        logger.debug("--- Row %d outer_html ---", i)
        logger.debug(row.evaluate("el => el.outerHTML"))
