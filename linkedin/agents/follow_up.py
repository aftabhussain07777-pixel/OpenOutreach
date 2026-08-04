# linkedin/agents/follow_up.py
"""Follow-up agent: reads conversation, returns a structured decision.

Single LLM call with structured output — no tool-calling loop.
The handler in tasks/follow_up.py executes the decision.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Literal

import jinja2
from pydantic import BaseModel, Field, model_validator
from pydantic_ai import Agent

from linkedin.conf import PROMPTS_DIR
from linkedin.llm import get_conversation_llm_model, run_agent_sync

logger = logging.getLogger(__name__)

class RecipientState(BaseModel):
    recognition: int = Field(ge=1, le=5)
    authenticity: int = Field(ge=1, le=5)
    relevance: int = Field(ge=1, le=5)
    cognitive_effort: int = Field(ge=1, le=5)
    conversation_willingness: int = Field(ge=1, le=5)


class ConversationState(BaseModel):
    topic: str
    momentum: Literal[
        "building",
        "stable",
        "fading",
        "looping",
    ]
    engagement: int = Field(ge=1, le=5)


class RelationshipState(BaseModel):
    familiarity: int = Field(ge=1, le=5)
    comfort: int = Field(ge=1, le=5)
    trust: int = Field(ge=1, le=5)


class BeliefState(BaseModel):
    about_self: str
    about_them: str
    about_our_connection: str
    shared_interests: str
    about_me: str


class ConversationStateSnapshot(BaseModel):
    recipient: RecipientState
    conversation: ConversationState
    relationship: RelationshipState
    belief: BeliefState

class Objective(BaseModel):
    category: Literal[
        "acknowledge",
        "understand",
        "continue",
        "clarify",
        "share",
        "answer",
        "wrap_up",
        "other",
    ]
    description: str

class FollowUpDecision(BaseModel):
    """Structured output from the follow-up agent."""

    action: Literal["send_message", "mark_completed", "wait"] = Field(
        description="What to do next for this lead.",
    )
    message: str | None = Field(
        default=None,
        description="The message to send. Required when action='send_message'.",
    )
    outcome: Literal[
        "converted", "not_interested", "wrong_fit", "no_budget",
        "has_solution", "bad_timing", "unresponsive",
    ] | None = Field(
        default=None,
        description="Why the conversation ended. Required when action='mark_completed'.",
    )
    follow_up_hours: float = Field(
        description="Hours until next follow-up. Always required — you decide the pace.",
    )
    user_states: ConversationStateSnapshot
    objective: Objective
    conversation_summary: str
    action_reason: str


    @model_validator(mode="after")
    def _check_required_fields(self):
        if self.action == "send_message" and not self.message:
            raise ValueError("message is required when action='send_message'")
        if self.action == "mark_completed" and not self.outcome:
            raise ValueError("outcome is required when action='mark_completed'")
        return self


# Number of trailing verbatim messages the agent sees alongside the rolling
# chat_summary. Older turns live in the summary fact list; the recency window
# preserves literal phrasing for the turns that matter most when composing
# the next reply.
RECENT_MESSAGES_WINDOW = 6


def _humanize_age(when: datetime, now: datetime) -> str:
    """Render `when` as a coarse age relative to `now` (e.g. ``3d ago``)."""
    delta = now - when
    if delta < timedelta(hours=1):
        return f"{max(int(delta.total_seconds() // 60), 1)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _format_recent_messages(messages: list, now: datetime) -> str:
    """Render the last few ChatMessage rows as a timestamped transcript."""
    if not messages:
        return "No recent messages."
    lines = []
    for m in messages:
        content = (m.content or "").strip()
        if not content:
            continue
        speaker = "Me" if m.is_outgoing else "Lead"
        prefix = f"{speaker} ({_humanize_age(m.creation_date, now)})" if m.creation_date else speaker
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines) or "No recent messages."


def _days_since_last_outgoing(messages: list, now: datetime) -> int | None:
    """Whole days since the most recent outgoing message, or None if there are none."""
    timestamps = [m.creation_date for m in messages if m.is_outgoing and m.creation_date]
    if not timestamps:
        return None
    return max((now - max(timestamps)).days, 0)


def _count_unanswered_outgoing(messages: list) -> int:
    """Trailing run of outgoing messages with no lead reply after them."""
    count = 0
    for m in reversed(messages):
        if m.is_outgoing:
            count += 1
        else:
            break
    return count


def _format_facts(summary: dict | None) -> str:
    """Render a `{facts: [...]}` summary blob as a bullet list."""
    facts = (summary or {}).get("facts") or []
    if not facts:
        return "(none yet)"
    return "\n".join(f"- {f}" for f in facts)


def _log_chat_facts(public_id: str, deal) -> None:
    """Log the mem0 chat facts the agent is working with."""
    chat_facts = (deal.chat_summary or {}).get("facts", [])
    if not chat_facts:
        return
    lines = [f"chat facts for {public_id}:"]
    lines.extend(f"  • {f}" for f in chat_facts)
    logger.info("\n".join(lines))


def _lead_has_replied(messages: list) -> bool:
    """True if the lead has sent any incoming messages."""
    return any(not m.is_outgoing for m in messages)


def _strip_dashes(text: str | None) -> str | None:
    """Remove em/en dashes the LLM produces despite the prompt rule.

    The prompt instructs the model to never use "—" or "–", but models
    still emit them. This is a deterministic post-process: the dash and any
    surrounding whitespace are collapsed to a single space.
    """
    if not text:
        return text
    return re.sub(r"\s*[\u2014\u2013]\s*", " ", text).strip()


def _load_recent_messages(deal, limit: int = RECENT_MESSAGES_WINDOW) -> list:
    """Last `limit` ChatMessages for `deal.lead`, in chronological order."""
    from chat.models import ChatMessage
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    qs = (
        ChatMessage.objects
        .filter(content_type=ct, object_id=deal.lead_id)
        .order_by("-creation_date", "-pk")[:limit]
    )
    return list(reversed(list(qs)))


def _resolve_prospect_name(deal, session) -> str:
    """Get the prospect's full name, preferring cached DB fields.

    First checks ``deal.lead.first_name`` / ``deal.lead.last_name`` (populated at
    enrichment time or lazily backfilled on re-scrape). If those are empty (e.g.
    legacy leads created before the fields existed), falls back to a Voyager
    scrape which will backfill them for next time. Final fallback is the
    LinkedIn public_identifier.
    """
    from django.db.models import Value as V
    from django.db.models.functions import Concat, Trim

    lead = deal.lead
    first = (lead.first_name or "").strip()
    last = (lead.last_name or "").strip()
    if first and last:
        return f"{first} {last}"
    if first:
        return first
    if last:
        return last

    # No cached name — scrape once to backfill
    try:
        profile = lead.get_profile(session)
        if profile:
            first = (profile.get("first_name") or "").strip()
            last = (profile.get("last_name") or "").strip()
            full = f"{first} {last}".strip()
            if full:
                return full
            if first:
                return first
            if last:
                return last
    except Exception:
        logger.debug(
            "Failed to resolve prospect name for %s, falling back to public_identifier",
            lead.public_identifier,
        )
    return lead.public_identifier


def _render_system_prompt(session, deal, recent_messages: list) -> str:
    """Render the appropriate agent system prompt based on conversation state.

    Uses ``outreach_agent.j2`` when the lead has not yet replied (no incoming
    messages), and ``conversation_agent.j2`` once the lead has engaged.
    """
    from django.utils import timezone

    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template_name = "conversation_agent.j2" if _lead_has_replied(recent_messages) else "outreach_agent.j2"
    template = env.get_template(template_name)

    campaign = deal.campaign
    self_prof = session.self_profile
    self_name = f"{self_prof.get('first_name', '')} {self_prof.get('last_name', '')}".strip() or session.django_user.username

    prospect_name = _resolve_prospect_name(deal, session)

    now = timezone.now()
    return template.render(
        self_name=self_name,
        prospect_name=prospect_name,
        contact_email=session.linkedin_profile.linkedin_username,
        product_docs=campaign.product_docs or "",
        campaign_objective=campaign.campaign_objective or "",
        booking_link=campaign.booking_link or "",
        profile_summary=_format_facts(deal.profile_summary),
        chat_summary=_format_facts(deal.chat_summary),
        recent_messages=_format_recent_messages(recent_messages, now),
        today=now.strftime("%Y-%m-%d"),
        days_since_last_outgoing=_days_since_last_outgoing(recent_messages, now),
        unanswered_outgoing=_count_unanswered_outgoing(recent_messages),
    )


def run_follow_up_agent(session, deal) -> FollowUpDecision:
    """Read conversation and return a structured follow-up decision.

    Sync chat first (which folds new messages into ``deal.chat_summary``),
    then render the prompt from the Deal's persistent summaries plus a small
    recency window of verbatim messages, and ask the LLM to decide.
    """
    from linkedin.db.chat import sync_conversation

    public_id = deal.lead.public_identifier
    sync_conversation(session, public_id)
    deal.refresh_from_db(fields=["chat_summary", "profile_summary"])
    _log_chat_facts(public_id, deal)

    recent = _load_recent_messages(deal)
    system_prompt = _render_system_prompt(session, deal, recent)

    agent = Agent(
        get_conversation_llm_model(),
        output_type=FollowUpDecision,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    decision = run_agent_sync(agent.run(system_prompt)).output
    if decision is None:
        raise RuntimeError(f"LLM returned unparseable response for follow-up of {public_id}")

    # Sanitize the message — strip em/en dashes programmatically since
    # the prompt-level rule is unreliable.
    decision.message = _strip_dashes(decision.message)

    logger.info("follow_up agent for %s: %s", public_id, decision.action)
    return decision


if __name__ == "__main__":
    from crm.models import Deal
    from linkedin.browser.registry import cli_parser, cli_session
    from linkedin.db.summaries import materialize_profile_summary_if_missing
    from linkedin.models import Task

    parser = cli_parser("Run the follow-up agent for a profile")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile", help="Public identifier of the target profile")
    group.add_argument("--task-id", type=int, help="Task ID to run the agent for")
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    if args.task_id:
        task = Task.objects.get(pk=args.task_id)
        public_id = task.payload["public_id"]
        campaign_id = task.payload["campaign_id"]
        from linkedin.models import Campaign
        campaign = Campaign.objects.get(pk=campaign_id)
        session.campaign = campaign
    else:
        public_id = args.profile

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id, campaign=session.campaign)
        .select_related("lead", "campaign")
        .first()
    )
    if not deal:
        logger.error("No Deal found for %s", public_id)
        raise SystemExit(1)

    logger.info("Running follow-up agent as %s for %s", session, public_id)
    logger.info("Campaign: %s", session.campaign)

    materialize_profile_summary_if_missing(deal, session)
    decision = run_follow_up_agent(session, deal)

    logger.info("Chat facts: %s", _format_facts(deal.chat_summary))
    logger.info("Action: %s", decision.action)
    if decision.message:
        logger.info("Message: %s", decision.message)
    if decision.outcome:
        logger.info("Outcome: %s", decision.outcome)
    logger.info("Follow-up in: %sh", decision.follow_up_hours)
