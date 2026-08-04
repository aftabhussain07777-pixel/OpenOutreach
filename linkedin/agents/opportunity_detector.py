# linkedin/agents/opportunity_detector.py
"""Opportunity detector: evaluates lead replies for business opportunities.

Runs after each new reply from a prospect. Returns a structured assessment
that determines whether the conversation merits a human-takeover notification.
"""

from __future__ import annotations

import logging
from datetime import datetime

import jinja2
from pydantic import BaseModel, Field

from pydantic_ai import Agent

from linkedin.conf import PROMPTS_DIR
from linkedin.llm import get_conversation_llm_model, run_agent_sync

logger = logging.getLogger(__name__)


class RecentMessage(BaseModel):
    """A single message from the conversation, derived from ChatMessage."""
    content: str
    is_outgoing: bool
    timestamp: datetime | None = None


class OpportunityAssessment(BaseModel):
    """Structured output from the opportunity detector."""

    opportunity_score: float = Field(ge=0.0, le=1.0, description="Overall likelihood of a worthwhile business opportunity")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in this assessment")
    conversation_readiness: float = Field(ge=0.0, le=1.0, description="Whether enough mutual understanding exists for a human to naturally join")
    problem_signal: float = Field(ge=0.0, le=1.0, description="Concrete evidence the prospect has a meaningful problem we solve")
    decision_influence: float = Field(ge=0.0, le=1.0, description="How likely this person can influence or approve solving that problem")
    openness: float = Field(ge=0.0, le=1.0, description="How willing the prospect appears to continue the conversation")
    notify_recommended: bool = Field(description="Whether this reply creates a good moment for human to personally join")
    opportunity_type: str = Field(description="Type of opportunity, or 'unknown' if none")
    evidence: list[str] = Field(description="Concrete observations supporting the assessment")
    summary: str = Field(description="Short objective summary of the assessment")


def _format_recent_messages(messages: list) -> str:
    """Render recent messages as a timestamped transcript."""
    if not messages:
        return "No recent messages."
    lines = []
    for m in messages:
        speaker = "Me" if m.is_outgoing else "Lead"
        prefix = f"{speaker}" if m.timestamp is None else f"{speaker} ({_humanize_age(m.timestamp)})"
        lines.append(f"{prefix}: {m.content}")
    return "\n".join(lines) or "No recent messages."


def _humanize_age(when: datetime) -> str:
    """Render a datetime as a coarse age relative to now."""
    from django.utils import timezone
    delta = timezone.now() - when
    if delta.total_seconds() < 3600:
        return f"{max(int(delta.total_seconds() // 60), 1)}m ago"
    if delta.days < 1:
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _format_section(data: dict | None, label: str) -> str:
    """Format a single state section as a readable block."""
    if not data:
        return f"No {label} data available."
    lines = []
    for k, v in data.items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines) if lines else f"No {label} data available."


def run_opportunity_detector(
    session,
    deal,
    *,
    latest_reply: str,
    recent_messages: list[RecentMessage],
) -> OpportunityAssessment:
    """Evaluate the latest prospect reply and return an opportunity assessment.

    Args:
        session: The current AccountSession.
        deal: The Deal being evaluated (reads ``agent_user_states`` from DB).
        latest_reply: The plain-text content of the latest prospect message.
        recent_messages: Recent conversation messages as ``RecentMessage`` objects.

    Returns:
        An ``OpportunityAssessment`` with scores, evidence, and notification recommendation.
    """
    from django.utils import timezone
    from chat.models import ChatMessage
    from linkedin.db.chat import sync_conversation

    campaign = deal.campaign

    # Sync to ensure fresh chat data
    public_id = deal.lead.public_identifier
    sync_conversation(session, public_id)
    deal.refresh_from_db(fields=["chat_summary", "profile_summary", "agent_user_states"])

    # Render prompt template
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(str(PROMPTS_DIR)))
    template = env.get_template("opportunity_detector.j2")

    # Format profile and chat summaries as bullet lists
    profile_facts = (deal.profile_summary or {}).get("facts") or []
    chat_facts = (deal.chat_summary or {}).get("facts") or []
    profile_summary_str = "\n".join(f"- {f}" for f in profile_facts) if profile_facts else "(none yet)"
    chat_summary_str = "\n".join(f"- {f}" for f in chat_facts) if chat_facts else "(none yet)"

    now = timezone.now()
    states = deal.agent_user_states or {}

    prompt = template.render(
        product_docs=campaign.product_docs or "",
        campaign_objective=campaign.campaign_objective or "",
        profile_summary=profile_summary_str,
        chat_summary=chat_summary_str,
        recent_messages=_format_recent_messages(recent_messages),
        latest_reply=latest_reply,
        conversation_state=_format_section(states.get("conversation"), "conversation"),
        relationship_state=_format_section(states.get("relationship"), "relationship"),
        recipient_state=_format_section(states.get("recipient"), "recipient"),
    )

    agent = Agent(
        get_conversation_llm_model(),
        output_type=OpportunityAssessment,
        model_settings={"temperature": 0.3, "timeout": 60},
    )
    result = run_agent_sync(agent.run(prompt)).output
    if result is None:
        raise RuntimeError(f"LLM returned unparseable opportunity assessment for {public_id}")

    logger.info(
        "opportunity_detector for %s: score=%.2f confidence=%.2f notify=%s type=%s",
        public_id, result.opportunity_score, result.confidence,
        result.notify_recommended, result.opportunity_type,
    )
    return result
