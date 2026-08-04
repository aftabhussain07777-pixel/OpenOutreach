# linkedin/simulation/simulation_runner.py
"""Simulation runner — orchestrates simulated conversations.

Creates real Django model instances (Campaign, Lead, Deal) and calls the
production ``run_follow_up_agent`` entry point directly — the same function
the daemon uses. The only difference is that LinkedIn API calls are replaced
with no-ops and the Prospect Simulator generates replies instead of a real
prospect.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from linkedin.simulation.scenario import Scenario
from linkedin.simulation.prospect_simulator import ProspectSimulator
from linkedin.simulation.transcript import Transcript, TranscriptTurn

logger = logging.getLogger(__name__)

MAX_TURNS = 10

SELLER_FIRST_NAME = "Sarah"
SELLER_LAST_NAME = "Miller"

SAMPLE_PROFILE = {
    "first_name": "UNSET",
    "last_name": "",
    "headline": "Professional",
    "positions": [{"company_name": "Unknown"}],
}


class _FakeSession:
    """Minimal stand-in for AccountSession for simulation."""

    def __init__(self, django_user, linkedin_profile, campaign):
        self.django_user = django_user
        self.linkedin_profile = linkedin_profile
        self.campaign = campaign
        self.self_profile = {
            "first_name": SELLER_FIRST_NAME,
            "last_name": SELLER_LAST_NAME,
            "urn": "urn:li:fsd_profile:SIM_SELLER",
        }

    @property
    def campaigns(self):
        from linkedin.models import Campaign
        return Campaign.objects.filter(users=self.django_user)

    def ensure_browser(self):
        pass


def _setup_db(scenario: Scenario):
    """Create Campaign, User, LinkedInProfile, Lead, and Deal in the DB.

    Returns a ``_FakeSession`` and the Deal for the simulation.
    """
    from django.contrib.auth.models import User
    from linkedin.management.setup_crm import setup_crm
    from linkedin.models import Campaign, LinkedInProfile
    from crm.models import Lead, Deal
    from linkedin.db.leads import create_enriched_lead, promote_lead_to_deal

    # Ensure CRM bootstrap data
    setup_crm()

    # Create or reuse campaign
    campaign, _ = Campaign.objects.get_or_create(
        name="Simulation Campaign",
        defaults={
            "product_docs": (
                "We provide AI-powered workflow automation for healthcare operations, "
                "including patient intake, scheduling, and billing reconciliation."
            ),
            "campaign_objective": (
                "Identify healthcare operations leaders interested in automation solutions."
            ),
            "booking_link": "https://calendly.com/sarah_miller/demo",
        },
    )

    # Create seller user
    user, _ = User.objects.get_or_create(
        username="sim_seller",
        defaults={"email": "sarah@example.com"},
    )
    campaign.users.add(user)

    # Create LinkedIn profile for seller
    linkedin_profile, _ = LinkedInProfile.objects.get_or_create(
        user=user,
        defaults={
            "linkedin_username": "sarah_miller_sales",
            "linkedin_password": "sim_pass",
        },
    )

    session = _FakeSession(
        django_user=user,
        linkedin_profile=linkedin_profile,
        campaign=campaign,
    )

    # Create lead from scenario
    profile = dict(SAMPLE_PROFILE)
    profile["first_name"] = scenario.name.split()[0] if " " in scenario.name else scenario.name
    profile["last_name"] = scenario.name.split(" ", 1)[-1] if " " in scenario.name else ""
    profile["headline"] = f"{scenario.role} at {scenario.company}"

    public_id = f"sim_{scenario.name.lower().replace(' ', '_')}"
    lead_pk = create_enriched_lead(session, f"https://linkedin.com/in/{public_id}/", profile)
    if lead_pk is None:
        # Lead already exists — find it
        lead = Lead.objects.get(public_identifier=public_id)
    else:
        lead = Lead.objects.get(pk=lead_pk)

    # Remove stale ChatMessages from previous runs
    from django.contrib.contenttypes.models import ContentType
    from chat.models import ChatMessage
    ct = ContentType.objects.get_for_model(Lead)
    ChatMessage.objects.filter(content_type=ct, object_id=lead.pk).delete()

    # Create or reuse deal
    deal = Deal.objects.filter(lead=lead, campaign=campaign).first()
    if deal is None:
        deal = promote_lead_to_deal(session, public_id)

    # Build profile_summary facts from scenario
    facts = [
        f"Name: {scenario.name}",
        f"Role: {scenario.role}",
        f"Company: {scenario.company}",
        f"Industry: {scenario.industry}",
        f"Location: {scenario.country}",
        f"Personality: {scenario.personality}",
        f"Communication style: {scenario.communication_style}",
        f"Priorities: {scenario.current_priorities}",
    ]
    deal.profile_summary = {"facts": facts}
    deal.chat_summary = {"facts": []}
    deal.save(update_fields=["profile_summary", "chat_summary"])

    return session, deal


def _build_transcript_turn(decision, turn_number, prospect_reply=""):
    """Build a TranscriptTurn from a FollowUpDecision."""
    return TranscriptTurn(
        turn_number=turn_number,
        prospect_reply=prospect_reply,
        agent_action=decision.action,
        agent_objective_category=decision.objective.category,
        agent_message=decision.message,
        agent_outcome=decision.outcome,
        recipient_state=(
            decision.user_states.recipient.model_dump()
            if decision.user_states else None
        ),
        conversation_state=(
            decision.user_states.conversation.model_dump()
            if decision.user_states else None
        ),
        relationship_state=(
            decision.user_states.relationship.model_dump()
            if decision.user_states else None
        ),
        belief_state=(
            decision.user_states.belief.model_dump()
            if decision.user_states else None
        ),
        conversation_summary=decision.conversation_summary or None,
        action_reason=decision.action_reason or None,
    )


def run_simulation(
    scenario: Scenario,
    *,
    max_turns: int = MAX_TURNS,
    transcript_path: str | Path | None = None,
) -> Transcript:
    """Run a full simulated conversation using the production agent.

    Creates real DB records and calls ``run_follow_up_agent`` directly —
    the same entry point the daemon uses. LinkedIn API calls (sync_conversation)
    are replaced with no-ops; the Prospect Simulator generates replies.

    The loop always lets the agent see the prospect's last reply before ending.
    """
    import linkedin.db.chat as chat_module
    from linkedin.tasks.follow_up import MAX_UNANSWERED_FOLLOW_UPS

    # Setup DB records
    session, deal = _setup_db(scenario)
    prospect = ProspectSimulator(scenario)
    public_id = deal.lead.public_identifier

    transcript = Transcript(scenario_name=scenario.name)
    conversation_history: list[dict] = []  # for prospect simulator
    prospect_ended = False
    unanswered_closed = False

    print(f"  Scenario: {scenario.name}")
    print(f"  Max turns: {max_turns}")
    print()

    for turn_count in range(1, max_turns + 1):
        # Refresh deal from DB so agent sees the latest chat_summary
        deal.refresh_from_db()

        print(f"  ── Turn {turn_count} ──")
        print(f"  Agent: thinking...", end="", flush=True)

        # ── Max-unanswered guard: close the loop instead of ──────────
        #     generating another message (mirrors production rule)
        if _unanswered_count(deal) >= MAX_UNANSWERED_FOLLOW_UPS:
            unanswered_closed = True
            transcript.add_turn(TranscriptTurn(
                turn_number=turn_count,
                prospect_reply="",
                agent_action="mark_completed",
                agent_objective_category="wrap_up",
                agent_message=None,
                agent_outcome="unresponsive",
                conversation_summary=f"{MAX_UNANSWERED_FOLLOW_UPS}+ unanswered messages — closed as unresponsive",
                action_reason="Max unanswered follow-ups reached — no further message generated",
            ))
            print(f"\r  Agent: mark_completed (unresponsive) — "
                  f"{MAX_UNANSWERED_FOLLOW_UPS}+ unanswered, closing loop")
            break

        # ── Call the production entry point ─────────────────────────
        # Monkey-patch sync_conversation to no-op (no LinkedIn API in simulation)
        original_sync = chat_module.sync_conversation
        chat_module.sync_conversation = lambda _session, _pid: None

        # Stub embeddings to avoid ONNX model dependency
        with patch("linkedin.ml.embeddings.embed_text", return_value=np.ones(384)):
            from linkedin.agents.follow_up import run_follow_up_agent
            decision = run_follow_up_agent(session, deal)

        chat_module.sync_conversation = original_sync

        # ── If prospect already ended, this was the agent's chance ──
        #     to see the last reply. Record and stop.
        if prospect_ended:
            transcript.add_turn(_build_transcript_turn(
                decision, turn_number=turn_count,
            ))
            print(f"\r  Agent: {decision.action} "
                  f"{f'(outcome={decision.outcome})' if decision.outcome else ''}")
            break

        # ── Agent ended the conversation ────────────────────────────
        if decision.action == "mark_completed":
            transcript.add_turn(_build_transcript_turn(
                decision, turn_number=turn_count,
            ))
            print(f"\r  Agent: {decision.action} (outcome={decision.outcome})")
            break

        # ── Agent sent a message → get prospect reply ───────────────
        if decision.action == "send_message" and decision.message:
            seller_msg = decision.message

            conversation_history.append({"role": "seller", "content": seller_msg})

            # Persist agent message as ChatMessage so the agent
            # sees it as "Me" on the next turn
            _persist_chat_message(deal, seller_msg, is_outgoing=True)

            print(f"\r  Agent: {decision.objective.category} \u2192 "
                  f"\"{seller_msg[:80]}{'...' if len(seller_msg) > 80 else ''}\"")
            print(f"  Prospect: thinking...", end="", flush=True)

            prospect_reply = prospect.generate_reply(
                conversation_history, seller_message=seller_msg,
            )

            conversation_history.append({"role": "prospect", "content": prospect_reply.reply})

            # Persist prospect reply as ChatMessage so the agent
            # sees it as "Lead" on the next turn
            _persist_chat_message(deal, prospect_reply.reply, is_outgoing=False)

            transcript.add_turn(_build_transcript_turn(
                decision, turn_number=turn_count,
                prospect_reply=prospect_reply.reply,
            ))

            print(f"\r  Prospect: \"{prospect_reply.reply[:80]}{'...' if len(prospect_reply.reply) > 80 else ''}\"")

            if not prospect_reply.continue_conversation:
                print(f"  Prospect stopped replying \u2014 letting agent respond.")
                prospect_ended = True
                # Continue to next iteration so agent sees this reply

        elif decision.action == "wait":
            print(f"\r  Agent: wait")
            transcript.add_turn(_build_transcript_turn(
                decision, turn_number=turn_count,
            ))
            break

        else:
            print(f"\r  Agent: no action (unexpected)")
            break

    # ── Write transcript ───────────────────────────────────────────
    if transcript_path is None:
        transcript_dir = Path(__file__).parent / "transcripts"
        transcript_dir.mkdir(exist_ok=True)
        safe_name = scenario.name.lower().replace(" ", "_").replace("/", "_")
        transcript_path = transcript_dir / f"{safe_name}.txt"

    print()
    transcript.write(transcript_path)
    print(f"  Transcript written to: {transcript_path}")
    print(f"  Simulation complete: {turn_count} turn(s)")
    if unanswered_closed:
        print(f"  Closed: max unanswered ({MAX_UNANSWERED_FOLLOW_UPS}) reached — no further message")
    elif transcript.ended_early:
        print(f"  Ended early (agent marked completed)")

    return transcript


_sim_msg_counter = 0


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


def _persist_chat_message(deal, content: str, is_outgoing: bool) -> None:
    """Save a message to the ChatMessage table so the agent sees it."""
    from django.contrib.contenttypes.models import ContentType
    from django.utils import timezone
    from chat.models import ChatMessage
    global _sim_msg_counter

    _sim_msg_counter += 1
    ct = ContentType.objects.get_for_model(deal.lead.__class__)
    ChatMessage.objects.create(
        content_type=ct,
        object_id=deal.lead_id,
        content=content,
        is_outgoing=is_outgoing,
        creation_date=timezone.now(),
        linkedin_urn=f"urn:li:sim_msg:{deal.lead_id}:{_sim_msg_counter}",
        source="ai",
    )
