"""Simulate a fresh-connection first message for the follow-up agent."""
import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")

import django
django.setup()

from crm.models import Deal
from linkedin.browser.registry import cli_parser, cli_session
from linkedin.db.summaries import materialize_profile_summary_if_missing
from linkedin.agents.follow_up import (
    run_follow_up_agent,
    _format_facts,
    _render_system_prompt,
    _load_recent_messages,
)
from linkedin.llm import get_conversation_llm_model, run_agent_sync
from pydantic_ai import Agent
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("follow_up_test")


if __name__ == "__main__":
    import argparse
    parser = cli_parser("Test follow-up agent (fresh connection)")
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()

    public_id = args.profile
    session = cli_session(args)

    deal = (
        Deal.objects.filter(lead__public_identifier=public_id)
        .select_related("lead", "campaign")
        .first()
    )
    if not deal:
        logger.error("No Deal found for %s", public_id)
        sys.exit(1)

    session.campaign = deal.campaign
    session.ensure_browser()

    logger.info("Simulating fresh connection for %s (%s)", public_id, deal.lead.first_name)

    # Materialize profile summary so we have context about who they are
    materialize_profile_summary_if_missing(deal, session)
    deal.refresh_from_db(fields=["profile_summary"])

    # Clear chat_summary to simulate no conversation history
    deal.chat_summary = None

    # Load ZERO recent messages to trigger First Message Principle
    recent_messages = []

    # Detect which template will be used
    from linkedin.agents.follow_up import _lead_has_replied
    template_used = "conversation_agent.j2" if _lead_has_replied(recent_messages) else "outreach_agent.j2"

    # Render what the agent will see
    system_prompt = _render_system_prompt(session, deal, recent_messages)

    print("\n" + "=" * 70)
    print(f"SYSTEM PROMPT (template: {template_used})")
    print("=" * 70)
    print(system_prompt)
    print("=" * 70)

    # Now run the LLM
    agent = Agent(
        get_conversation_llm_model(),
        output_type=__import__("linkedin.agents.follow_up", fromlist=["FollowUpDecision"]).FollowUpDecision,
        model_settings={"temperature": 0.7, "timeout": 60},
    )
    decision = run_agent_sync(agent.run(system_prompt)).output

    if decision is None:
        logger.error("LLM returned unparseable response")
        sys.exit(1)

    print("\n" + "=" * 70)
    print("FOLLOW-UP AGENT RESULT (Fresh Connection)")
    print("=" * 70)
    print(f"Action:              {decision.action}")
    print(f"Follow-up in:        {decision.follow_up_hours}h")
    print(f"Outcome:             {decision.outcome}")
    print(f"Objective:           {decision.objective}")
    print(f"Objective Category:  {decision.objective_category}")
    if decision.message:
        print(f"\n{'─' * 70}")
        print("GENERATED MESSAGE:")
        print(f"{'─' * 70}")
        print()
        print(decision.message)
        print()
    print(f"\n{'─' * 70}")
    print("User States:")
    for k, v in decision.user_states.model_dump().items():
        print(f"  {k}: {v}")
    print(f"{'─' * 70}")
    print(f"Reasoning:")
    print(decision.reasoning_summary)
    print("=" * 70)
