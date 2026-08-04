# linkedin/simulation/unanswered_sweep.py
"""Interval sweep — test what the follow-up agent generates across multiple
time gaps (e.g. 3, 7, 10 days), in two modes:

- **No-reply mode** (default): the prospect never replies. Uses
  ``outreach_agent.j2`` (First Message / First Follow-up / Final Follow-up
  principles). When the unanswered count reaches ``MAX_UNANSWERED_FOLLOW_UPS``
  (3), the deal is marked COMPLETED as unresponsive and the loop closes.

- **Reply mode** (``dummy_reply`` set): the prospect replies once with a
  synthetic message, so ``conversation_agent.j2`` is used at each interval.
  Tests how the agent behaves when a replied conversation goes quiet.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from linkedin.tasks.follow_up import MAX_UNANSWERED_FOLLOW_UPS

from linkedin.simulation.scenario import Scenario
from linkedin.simulation.simulation_runner import _setup_db, _persist_chat_message

logger = logging.getLogger(__name__)

DEFAULT_DAY_FRAMES = [3, 7, 10]

DEFAULT_DUMMY_REPLY = (
    "Thanks for reaching out. We're stretched thin at the moment, "
    "but always curious about what's changing in healthcare ops."
)


def _clear_messages(deal) -> None:
    """Delete all ChatMessages for the deal's lead."""
    from django.contrib.contenttypes.models import ContentType
    from chat.models import ChatMessage

    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    ChatMessage.objects.filter(content_type=ct, object_id=deal.lead_id).delete()
    deal.chat_summary = {"facts": []}
    deal.save(update_fields=["chat_summary"])


def _unanswered_count(deal) -> int:
    """Trailing run of outgoing messages with no lead reply after them."""
    from django.contrib.contenttypes.models import ContentType
    from chat.models import ChatMessage

    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    msgs = list(
        ChatMessage.objects.filter(
            content_type=ct, object_id=deal.lead_id,
        ).order_by("creation_date", "pk")
    )
    count = 0
    for m in reversed(msgs):
        if m.is_outgoing:
            count += 1
        else:
            break
    return count


def _run_agent(session, deal):
    """Run the production follow-up agent with LinkedIn calls stubbed."""
    import linkedin.db.chat as chat_module
    from unittest.mock import patch

    import numpy as np

    original_sync = chat_module.sync_conversation
    chat_module.sync_conversation = lambda _s, _p: None
    try:
        with patch("linkedin.ml.embeddings.embed_text", return_value=np.ones(384)):
            from linkedin.agents.follow_up import run_follow_up_agent
            decision = run_follow_up_agent(session, deal)
    finally:
        chat_module.sync_conversation = original_sync
    return decision


def _generate_first_message(session, deal) -> str:
    """Ask the production agent for the initial outreach message."""
    _clear_messages(deal)
    decision = _run_agent(session, deal)
    if decision.action == "send_message" and decision.message:
        return decision.message
    # Fallback if the agent decides not to message first (unlikely)
    return (
        f"Hi {deal.lead.first_name}, noticed your work on operations at "
        f"{deal.campaign.product_docs[:40]}... happy to share what we do."
    )


def _build_chain(
    first_message: str,
    dummy_reply: str | None,
    sent_messages: list[str],
    day_frames: list[int],
) -> list[tuple[str, bool, int]]:
    """Build the conversation chain as ``(content, is_outgoing, sent_at_day)``.

    Timeline:
    - first message sent day 0
    - dummy prospect reply (if any) sent day 1
    - each follow-up sent at its frame boundary (day_frames[0], [1], ...)
    """
    chain: list[tuple[str, bool, int]] = [(first_message, True, 0)]
    if dummy_reply is not None:
        chain.append((dummy_reply, False, 1))
    for idx, msg in enumerate(sent_messages):
        chain.append((msg, True, day_frames[idx]))
    return chain


def _build_conversation(deal, chain: list[tuple[str, bool, int]], days: int) -> None:
    """Rebuild the conversation with each message aged to the frame day."""
    _clear_messages(deal)
    for content, is_outgoing, sent_at in chain:
        age_days = max(days - sent_at, 0)
        _persist_chat_message(deal, content, is_outgoing)
        _backdate_last_message(deal, age_days)


def _backdate_last_message(deal, days: int) -> None:
    """Set the most recent message's creation_date to N days ago."""
    from django.contrib.contenttypes.models import ContentType
    from chat.models import ChatMessage

    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    ChatMessage.objects.filter(
        content_type=ct,
        object_id=deal.lead_id,
    ).update(creation_date=timezone.now() - timedelta(days=days))


def run_interval_sweep(
    scenario: Scenario,
    *,
    day_frames: list[int] | None = None,
    dummy_reply: str | None = None,
    output_path: str | Path | None = None,
) -> dict[int, dict]:
    """Run the interval sweep across multiple day frames (cumulative).

    Args:
        scenario: The prospect scenario.
        day_frames: Days-after-send to evaluate (default ``[3, 7, 10]``).
            Must be strictly increasing.
        dummy_reply: Optional synthetic prospect reply. When set, the agent
            uses ``conversation_agent.j2``; otherwise ``outreach_agent.j2``.
        output_path: Where to write the comparison report.

    Returns:
        ``{day: {action, objective, message, outcome, reason, summary}}``.
    """
    if day_frames is None:
        day_frames = DEFAULT_DAY_FRAMES
    day_frames = sorted(set(day_frames))
    if len(day_frames) < 2:
        raise ValueError("Provide at least two day frames for a cumulative sweep.")

    session, deal = _setup_db(scenario)
    public_id = deal.lead.public_identifier
    mode = "conversation_agent.j2" if dummy_reply is not None else "outreach_agent.j2"

    # Step 1: generate the initial outreach with the production agent
    print(f"  Generating initial outreach for {public_id} ...")
    first_message = _generate_first_message(session, deal)
    print(f"  Initial outreach: \"{first_message[:80]}{'...' if len(first_message) > 80 else ''}\"")
    if dummy_reply is not None:
        print(f"  Dummy prospect reply: \"{dummy_reply[:80]}{'...' if len(dummy_reply) > 80 else ''}\"")
    print(f"  Template: {mode}")
    print()

    results: dict[int, dict] = {}
    sent_messages: list[str] = []
    closed = False
    closed_at_days: int | None = None
    max_unanswered = MAX_UNANSWERED_FOLLOW_UPS

    for idx, days in enumerate(day_frames):
        print(f"  ── {days} days since last message ──")

        if closed:
            results[days] = {
                "action": "mark_completed",
                "objective": "wrap_up",
                "objective_description": "Conversation closed",
                "message": None,
                "outcome": "unresponsive",
                "reason": f"Reached max unanswered ({max_unanswered}) — loop closed",
                "summary": "No reply received — loop closed",
                "closed": True,
            }
            print(f"  (already closed at {closed_at_days} days — no message generated)")
            continue

        chain = _build_chain(first_message, dummy_reply, sent_messages, day_frames)
        _build_conversation(deal, chain, days)

        # Enforce the cap BEFORE generating: 3 unanswered is the max
        unanswered = _unanswered_count(deal)
        if unanswered >= max_unanswered:
            closed = True
            closed_at_days = days
            results[days] = {
                "action": "mark_completed",
                "objective": "wrap_up",
                "objective_description": "Conversation closed",
                "message": None,
                "outcome": "unresponsive",
                "reason": f"Unanswered count ({unanswered}) reached max {max_unanswered} — "
                          "deal marked COMPLETED, no further message",
                "summary": f"{unanswered} unanswered message(s) — no reply received",
                "closed": True,
            }
            print(f"  → {unanswered} unanswered messages = max {max_unanswered} — "
                  f"marking COMPLETED (unresponsive), no message generated")
            continue

        print(f"  Agent: thinking...", end="", flush=True)
        deal.refresh_from_db()
        decision = _run_agent(session, deal)

        result = {
            "action": decision.action,
            "objective": decision.objective.category,
            "objective_description": decision.objective.description,
            "message": decision.message,
            "outcome": decision.outcome,
            "reason": decision.action_reason,
            "summary": decision.conversation_summary,
            "closed": False,
        }
        results[days] = result

        print(f"\r  Agent: {decision.action} "
              f"{f'(outcome={decision.outcome})' if decision.outcome else ''}")
        if decision.message:
            print(f"  → \"{decision.message[:120]}{'...' if len(decision.message) > 120 else ''}\"")

        if decision.action == "send_message" and decision.message:
            sent_messages.append(decision.message)
        elif decision.action == "mark_completed":
            closed = True
            closed_at_days = days
        print()

    # Write comparison report
    if output_path is None:
        out_dir = Path(__file__).parent / "transcripts"
        out_dir.mkdir(exist_ok=True)
        safe_name = scenario.name.lower().replace(" ", "_").replace("/", "_")
        suffix = "_reply" if dummy_reply is not None else "_unanswered"
        output_path = out_dir / f"{safe_name}{suffix}.txt"

    _write_report(Path(output_path), scenario, first_message, dummy_reply, mode, results)
    print(f"  Report written to: {output_path}")

    return results


def run_unanswered_sweep(scenario, **kwargs) -> dict[int, dict]:
    """No-reply mode (outreach_agent.j2). See ``run_interval_sweep``."""
    kwargs.pop("dummy_reply", None)
    return run_interval_sweep(scenario, dummy_reply=None, **kwargs)


def run_reply_sweep(scenario, *, dummy_reply: str = DEFAULT_DUMMY_REPLY, **kwargs) -> dict[int, dict]:
    """Reply mode (conversation_agent.j2). See ``run_interval_sweep``."""
    return run_interval_sweep(scenario, dummy_reply=dummy_reply, **kwargs)


def _write_report(
    path: Path,
    scenario: Scenario,
    first_message: str,
    dummy_reply: str | None,
    mode: str,
    results: dict[int, dict],
) -> None:
    """Write the comparison report to a text file."""
    lines = [
        "=" * 60,
        "Interval Sweep (cumulative)",
        "=" * 60,
        "",
        f"Scenario: {scenario.name}",
        f"Role: {scenario.role}",
        f"Company: {scenario.company}",
        "",
        f"Template: {mode}",
        f"Max unanswered before close: {MAX_UNANSWERED_FOLLOW_UPS}",
        "",
        "Initial Outreach Message:",
        f"  \"{first_message}\"",
    ]
    if dummy_reply is not None:
        lines.extend([
            "",
            "Dummy Prospect Reply:",
            f"  \"{dummy_reply}\"",
        ])
    lines.append("")

    for days in sorted(results):
        r = results[days]
        lines.extend([
            "=" * 60,
            f"{days} days since last message",
            "=" * 60,
            "",
            f"  Action: {r['action']}",
            f"  Objective: {r['objective']} — {r['objective_description']}",
        ])
        if r.get("message"):
            lines.append(f"  Message: {r['message']}")
        if r.get("outcome"):
            lines.append(f"  Outcome: {r['outcome']}")
        lines.extend([
            f"  Action Reason: {r['reason']}",
            f"  Conversation Summary: {r['summary']}",
            "",
        ])

    path.write_text("\n".join(lines))
