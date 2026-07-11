# linkedin/actions/connect.py
import logging
from typing import Any, Dict

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from linkedin.browser.nav import dump_page_html, find_top_card
from linkedin.enums import ProfileState
from linkedin.exceptions import ReachedConnectionLimit, SkipProfile

logger = logging.getLogger(__name__)

SELECTORS = {
    "weekly_limit": 'div[class*="ip-fuse-limit-alert__warning"]',
    "invite_to_connect": (
        '[aria-label*="Invite"][aria-label*="to connect"]:visible, '
        'a:has(span:text-is("Connect")):visible, '
        'button:has(span:text-is("Connect")):visible'
    ),
    "error_toast": 'div[data-test-artdeco-toast-item-type="error"]',
    "withdrawn_error": 'p:text-is("Invitation not sent"), p:has-text("resend an invitation")',
    "more_button": (
        'button[aria-label="More"]:visible, '
        'button[id*="overflow"]:visible, '
        'button[aria-label*="More actions"]:visible, '
        'button:has(span:text-is("More")):visible'
    ),
    "connect_option": (
        'div[role="button"][aria-label^="Invite"][aria-label*=" to connect"], '
        'div[role="button"]:text-is("Connect"), '
        '[role="menuitem"][aria-label*="Connect"], '
        '[role="menuitem"]:has-text("Connect"), '
        'li:text-is("Connect"), '
        'span[role="button"]:text-is("Connect")'
    ),
    "send_now": 'button:has-text("Send now"), button[aria-label*="Send without"], button[aria-label*="Send invitation"]',
    "dialog_close": 'button[aria-label="Close"]:visible',
    "email_verify_modal": 'div[data-test-modal].send-invite input[name="email"]:visible',
    "email_verify_dismiss": 'button[aria-label="Dismiss"]:visible',
}


def send_connection_request(
    session: "AccountSession",
    profile: Dict[str, Any],
) -> ProfileState:
    """
    Sends a LinkedIn connection request WITHOUT a note (fastest & safest).

    Assumes the profile page is already loaded (caller navigates via
    ``get_connection_status`` or ``visit_profile`` beforehand).
    """
    public_identifier = profile.get("public_identifier")

    # Send invitation WITHOUT note (current active flow)
    if not _connect_direct(session) and not _connect_via_more(session):
        logger.debug(
            "Connect button not found for %s — staying at current stage",
            public_identifier,
        )
        dump_page_html(session, profile)
        return ProfileState.QUALIFIED

    _click_without_note(session)
    _check_weekly_invitation_limit(session)

    logger.debug("Connection request submitted for %s", public_identifier)
    return ProfileState.PENDING


def _check_weekly_invitation_limit(session):
    weekly_invitation_limit = session.page.locator(SELECTORS["weekly_limit"])
    if weekly_invitation_limit.count() > 0:
        raise ReachedConnectionLimit("Weekly connection limit pop up appeared")


def _connect_direct(session):
    session.wait()
    top_card = find_top_card(session)
    direct = top_card.locator(SELECTORS["invite_to_connect"])
    if direct.count() == 0:
        return False

    direct.first.dispatch_event("click")
    logger.debug("Dispatched click on direct 'Connect' button")

    error = session.page.locator(SELECTORS["error_toast"])
    if error.count() > 0:
        raise SkipProfile(f"{error.inner_text().strip()}")

    return True


def _connect_via_more(session):
    session.wait()
    top_card = find_top_card(session)
    page = session.page

    # Dropdown may render as a portal outside top_card, so search page-wide
    connect_option = page.locator(SELECTORS["connect_option"])

    # Connect option may already be visible (More dropdown opened by status check)
    if connect_option.count() == 0:
        more = top_card.locator(SELECTORS["more_button"])
        if more.count() == 0:
            return False
        more.first.click()
        session.wait()

    connect_option = page.locator(SELECTORS["connect_option"])
    if connect_option.count() == 0:
        return False
    connect_option.first.dispatch_event("click")
    logger.debug("Dispatched click on 'More → Connect' option")

    return True


def _click_without_note(session):
    """Click flow: sends connection request instantly without note."""
    session.wait()

    # Wait for the send-invite modal to appear after clicking Connect.
    # If it doesn't appear within 15 seconds, the click didn't register —
    # skip this lead instead of burning 30s on the send-now locator.
    modal = session.page.locator(".send-invite")
    try:
        modal.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError:
        logger.warning("Connect modal did not appear after clicking Connect")
        dump_page_html(
            session, {"public_identifier": "unknown"}, category="connect_no_modal"
        )
        raise SkipProfile("Connect modal did not appear")

    # Check for email verification popup — LinkedIn asks for the lead's email
    # to confirm you know them. We don't have that, so skip this lead.
    email_verify = session.page.locator(SELECTORS["email_verify_modal"])
    if email_verify.count() > 0:
        logger.warning("Email verification required — dismissing and skipping lead")
        dismiss_btn = session.page.locator(SELECTORS["email_verify_dismiss"])
        if dismiss_btn.count() > 0:
            dismiss_btn.first.click()
            session.wait()
        raise SkipProfile("Email verification required to connect with this lead")

    # Check for "Invitation not sent" error (manually withdrawn request)
    withdrawn = session.page.locator(SELECTORS["withdrawn_error"])
    if withdrawn.count() > 0:
        msg = withdrawn.first.inner_text().strip()
        logger.warning("Withdrawn invitation error: %s", msg)
        # Close the dialog so the page is left in a clean state
        close_btn = session.page.locator(SELECTORS["dialog_close"])
        if close_btn.count() > 0:
            close_btn.first.click()
            session.wait()
        raise SkipProfile(msg)

    # Click "Send now" / "Send without a note"
    send_btn = session.page.locator(SELECTORS["send_now"])
    send_btn.first.click(force=True)
    session.wait()
    logger.debug("Connection request submitted (no note)")


if __name__ == "__main__":
    from linkedin.actions.status import get_connection_status
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Send a LinkedIn connection request")
    parser.add_argument(
        "--profile", required=True, help="Public identifier of the target profile"
    )
    args = parser.parse_args()
    session = cli_session(args)

    test_profile = {
        "url": f"https://www.linkedin.com/in/{args.profile}/",
        "public_identifier": args.profile,
    }

    logger.info("Testing connection request as %s → %s", session, args.profile)

    connection_status = get_connection_status(session, test_profile)
    logger.info("Pre-check status → %s", connection_status.value)

    if connection_status in (ProfileState.CONNECTED, ProfileState.PENDING):
        logger.info("Skipping – already %s", connection_status.value)
    else:
        status = send_connection_request(session=session, profile=test_profile)
        logger.info("Finished → Status: %s", status.value)
