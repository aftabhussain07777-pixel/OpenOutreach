# linkedin/simulation/opportunity_test.py
"""Opportunity detector test harness.

Runs the full opportunity pipeline against a scripted conversation:
1. Builds a realistic exchange (agent outreach, prospect replies, and a
   final reply with strong opportunity signals).
2. Runs the production follow-up agent to populate ``agent_user_states``
   and show what it WOULD reply.
3. Runs the production opportunity detector on the latest reply.
4. Prints the full assessment, evaluates the notification triggers
   (``notify_recommended`` OR ``opportunity_score >= threshold``), sends
   the Telegram notification, and flags the deal.
5. Demonstrates the halt: with ``deal.opportunity_flagged = True`` the
   handler guard blocks the follow-up agent from sending anything.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path

from linkedin.simulation.scenario import Scenario
from linkedin.simulation.simulation_runner import (
    _setup_db,
    _persist_chat_message,
)

logger = logging.getLogger(__name__)

DEFAULT_PRIOR_REPLY = (
    "Thanks for reaching out. Curious what kind of automation "
    "you're referring to."
)

DEFAULT_LATEST_REPLY = (
    "Interesting, we've actually been fighting patient intake bottlenecks "
    "for the better part of a year now. How does your system handle HIPAA "
    "compliance, and do you have any examples from hospitals around our size? "
    "Happy to look at how this would work."
)


def _stub_agent_call():
    """Context manager that stubs LinkedIn + embedding calls."""
    import linkedin.db.chat as chat_module
    from unittest.mock import patch

    import numpy as np

    original_sync = chat_module.sync_conversation
    chat_module.sync_conversation = lambda _s, _p: None
    try:
        with patch("linkedin.ml.embeddings.embed_text", return_value=np.ones(384)):
            yield
    finally:
        chat_module.sync_conversation = original_sync


_stub_agent_call = contextmanager(_stub_agent_call)


def _run_follow_up(session, deal):
    """Run the production follow-up agent with LinkedIn calls stubbed."""
    from linkedin.agents.follow_up import run_follow_up_agent

    with _stub_agent_call():
        return run_follow_up_agent(session, deal)


def _run_detector(session, deal, latest_reply: str, recent_msgs: list):
    """Run the production opportunity detector with LinkedIn calls stubbed."""
    from linkedin.agents.opportunity_detector import (
        run_opportunity_detector, RecentMessage,
    )

    with _stub_agent_call():
        return run_opportunity_detector(
            session, deal,
            latest_reply=latest_reply,
            recent_messages=recent_msgs,
        )


def _load_recent_msgs(deal) -> list:
    """Load the deal's ChatMessages as detector RecentMessage objects."""
    from django.contrib.contenttypes.models import ContentType
    from chat.models import ChatMessage
    from linkedin.agents.opportunity_detector import RecentMessage

    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    msgs = list(
        ChatMessage.objects.filter(
            content_type=ct, object_id=deal.lead_id,
        ).order_by("creation_date", "pk")
    )
    return [
        RecentMessage(
            content=m.content or "",
            is_outgoing=m.is_outgoing,
            timestamp=m.creation_date,
        )
        for m in msgs
    ]


def run_opportunity_test(
    scenario: Scenario,
    *,
    prior_reply: str = DEFAULT_PRIOR_REPLY,
    latest_reply: str = DEFAULT_LATEST_REPLY,
    send_notification: bool = True,
) -> dict:
    """Run the opportunity pipeline test.

    Args:
        scenario: The prospect scenario.
        prior_reply: The prospect's earlier reply (before the strong signal).
        latest_reply: The latest prospect reply with opportunity signals.
        send_notification: Whether to actually send the Telegram notification
            (``False`` = preview only).

    Returns:
        A dict with the assessment, trigger results, and halt state.
    """
    session, deal = _setup_db(scenario)
    public_id = deal.lead.public_identifier

    # ── 1. Build the conversation ───────────────────────────────────
    print(f"  Scenario: {scenario.name} ({public_id})")
    print()

    # Outreach (empty conversation → outreach_agent.j2)
    first = _run_follow_up(session, deal)
    if first.action != "send_message" or not first.message:
        raise RuntimeError("Agent did not produce an initial outreach message")
    _persist_chat_message(deal, first.message, is_outgoing=True)

    # Prospect's earlier reply
    _persist_chat_message(deal, prior_reply, is_outgoing=False)

    # Agent replies to the earlier reply (populates agent_user_states)
    deal.refresh_from_db()
    second = _run_follow_up(session, deal)
    if second.action == "send_message" and second.message:
        deal.agent_user_states = second.user_states.model_dump()
        deal.save(update_fields=["agent_user_states"])
        _persist_chat_message(deal, second.message, is_outgoing=True)

    # Latest reply (the strong opportunity signal)
    _persist_chat_message(deal, latest_reply, is_outgoing=False)

    # ── 2. Show the hypothetical agent reply (what gets blocked) ────
    deal.refresh_from_db()
    hypothetical = _run_follow_up(session, deal)
    print("  Conversation transcript:")
    for i, m in enumerate(_load_recent_msgs(deal)):
        speaker = "Agent" if m.is_outgoing else "Prospect"
        print(f"    {i + 1}. {speaker}: {m.content}")
    print()
    print("  Follow-up agent (hypothetical, before flag):")
    print(f"    action={hypothetical.action}")
    if hypothetical.message:
        print(f"    message=\"{hypothetical.message}\"")
    print()

    # ── 3. Run the opportunity detector ─────────────────────────────
    recent_msgs = _load_recent_msgs(deal)
    assessment = _run_detector(session, deal, latest_reply, recent_msgs)

    print("  ── Opportunity Assessment ──")
    print(f"    Opportunity Score:      {assessment.opportunity_score:.2f}")
    print(f"    Confidence:             {assessment.confidence:.2f}")
    print(f"    Conversation Readiness: {assessment.conversation_readiness:.2f}")
    print(f"    Problem Signal:         {assessment.problem_signal:.2f}")
    print(f"    Decision Influence:     {assessment.decision_influence:.2f}")
    print(f"    Openness:               {assessment.openness:.2f}")
    print(f"    Notify Recommended:     {assessment.notify_recommended}")
    print(f"    Opportunity Type:       {assessment.opportunity_type}")
    for e in assessment.evidence:
        print(f"    Evidence:               - {e}")
    print(f"    Summary:                {assessment.summary}")
    print()

    # ── 4. Evaluate triggers ────────────────────────────────────────
    from linkedin.models import SiteConfig

    threshold = SiteConfig.load().opportunity_score_threshold
    triggers = []
    if assessment.notify_recommended:
        triggers.append("notify_recommended=True")
    if assessment.opportunity_score >= threshold:
        triggers.append(f"score {assessment.opportunity_score:.2f} >= threshold {threshold}")
    triggered = bool(triggers)

    print("  ── Notification Triggers ──")
    if triggers:
        for t in triggers:
            print(f"    ✓ {t}")
    else:
        print(f"    ✗ none (score {assessment.opportunity_score:.2f} < threshold {threshold})")
    print()

    deal.opportunity_assessment = assessment.model_dump()

    if triggered:
        if send_notification:
            from linkedin.notification import notify_opportunity

            sent = notify_opportunity(
                campaign=str(session.campaign),
                lead=public_id,
                opportunity_score=assessment.opportunity_score,
                threshold=threshold,
                notify_recommended=assessment.notify_recommended,
                opportunity_type=assessment.opportunity_type,
                summary=assessment.summary,
                evidence=assessment.evidence,
            )
            print(f"  → Telegram notification sent: {sent}")
        else:
            print("  → [preview] Telegram notification would be sent (--no-send)")

        deal.opportunity_flagged = True
        deal.save(update_fields=["opportunity_assessment", "opportunity_flagged"])
        print(f"  → deal.opportunity_flagged = True (human takeover)")
    else:
        deal.save(update_fields=["opportunity_assessment"])
        print("  → no notification, deal NOT flagged")

    print()

    # ── 5. Halt check ───────────────────────────────────────────────
    print("  ── HALT CHECK ──")
    if deal.opportunity_flagged:
        print("  deal.opportunity_flagged = True")
        print("  → handler guard BLOCKS the follow-up agent:")
        print("    • nudge path:     _handle_follow_up_nudge returns early")
        print("    • lead-reply path: skips auto-reply")
        print(f"  Hypothetical agent message NOT sent: \"{hypothetical.message or '(none)'}\"")
    else:
        print("  deal.opportunity_flagged = False — agent continues normally")
        if hypothetical.action == "send_message" and hypothetical.message:
            print(f"  → agent would send: \"{hypothetical.message}\"")
    print()

    return {
        "assessment": assessment.model_dump(),
        "triggers": triggers,
        "triggered": triggered,
        "flagged": deal.opportunity_flagged,
        "hypothetical_agent_message": hypothetical.message,
    }
