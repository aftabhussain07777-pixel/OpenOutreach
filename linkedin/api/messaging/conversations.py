# linkedin/api/messaging/conversations.py
"""Retrieve conversations and messages via Voyager Messaging GraphQL API."""

import logging

from playwright._impl._errors import Error as PlaywrightError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from linkedin.api.client import PlaywrightLinkedinAPI
from linkedin.api.messaging.utils import check_response, encode_urn

logger = logging.getLogger(__name__)


def _retryable(exception: BaseException) -> bool:
    """Retry on IOError (HTTP/network) or Playwright navigation errors."""
    if isinstance(exception, IOError):
        return True
    if isinstance(exception, PlaywrightError) and (
        "Execution context was destroyed" in str(exception)
        or "navigation" in str(exception).lower()
    ):
        return True
    return False


_GRAPHQL_BASE = "https://www.linkedin.com/voyager/api/voyagerMessagingGraphQL/graphql"
_CONVERSATIONS_QUERY_ID = "messengerConversations.0d5e6781bbee71c3e51c8843c6519f48"
_MESSAGES_QUERY_ID = "messengerMessages.5846eeb71c981f11e0134cb6626cc314"


def _graphql_headers(api: PlaywrightLinkedinAPI) -> dict:
    headers = {**api.headers}
    headers["accept"] = "application/graphql"
    return headers


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(_retryable),
    reraise=True,
)
def fetch_conversations(api: PlaywrightLinkedinAPI, mailbox_urn: str) -> dict:
    """Fetch recent conversations list. Returns raw API response."""
    url = (
        f"{_GRAPHQL_BASE}"
        f"?queryId={_CONVERSATIONS_QUERY_ID}"
        f"&variables=(mailboxUrn:{encode_urn(mailbox_urn)})"
    )
    res = api.get(url, headers=_graphql_headers(api))
    check_response(res, "fetch_conversations")
    return res.json()


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    retry=retry_if_exception_type(_retryable),
    reraise=True,
)
def fetch_messages(api: PlaywrightLinkedinAPI, conversation_urn: str) -> dict:
    """Fetch messages for a conversation. Returns raw API response."""
    url = (
        f"{_GRAPHQL_BASE}"
        f"?queryId={_MESSAGES_QUERY_ID}"
        f"&variables=(conversationUrn:{encode_urn(conversation_urn)})"
    )
    res = api.get(url, headers=_graphql_headers(api))
    check_response(res, "fetch_messages")
    return res.json()


if __name__ == "__main__":
    import json

    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Fetch raw Voyager messaging data")
    parser.add_argument(
        "--conversations", action="store_true", help="List recent conversations"
    )
    parser.add_argument(
        "--messages",
        default=None,
        metavar="CONVERSATION_URN",
        help="Fetch messages for a conversation URN",
    )
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    api = PlaywrightLinkedinAPI(session=session)

    if args.conversations:
        mailbox_urn = session.self_profile["urn"]
        raw = fetch_conversations(api, mailbox_urn)
        elements = (
            raw.get("data", {})
            .get("messengerConversationsBySyncToken", {})
            .get("elements", [])
        )
        logger.info("Got %d conversations:", len(elements))
        for conv in elements:
            urn = conv.get("entityUrn", "")
            participants = []
            for p in conv.get("conversationParticipants", []):
                member = p.get("participantType", {}).get("member", {})
                first = (member.get("firstName") or {}).get("text", "")
                last = (member.get("lastName") or {}).get("text", "")
                name = f"{first} {last}".strip()
                if name:
                    participants.append(name)
            logger.info("  %s", ", ".join(participants))
            logger.debug("    URN: %s", urn)

    elif args.messages:
        raw = fetch_messages(api, args.messages)
        logger.info(json.dumps(raw, indent=2, default=str)[:10000])

    else:
        parser.print_help()
