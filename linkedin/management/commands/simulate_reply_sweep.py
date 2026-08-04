# linkedin/management/commands/simulate_reply_sweep.py
"""Management command for the reply-mode interval sweep.

Tests what the follow-up agent generates at multiple time gaps (3/7/10 days)
in an active conversation — the prospect has replied once, so
``conversation_agent.j2`` is used (not ``outreach_agent.j2``).

Usage:
    python manage.py simulate_reply_sweep
    python manage.py simulate_reply_sweep --reply "Thanks, will look into it."
    python manage.py simulate_reply_sweep --days 3 --days 7 --days 10 --days 14
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from linkedin.simulation.scenario import default_scenario, load_scenario
from linkedin.simulation.unanswered_sweep import (
    DEFAULT_DAY_FRAMES,
    DEFAULT_DUMMY_REPLY,
    run_reply_sweep,
)


class Command(BaseCommand):
    help = (
        "Run the interval sweep with conversation_agent.j2 — the prospect has "
        "replied once, test what the agent generates after 3/7/10 quiet days."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scenario", "-s",
            default=None,
            help="Path to a scenario JSON file. Defaults to the built-in sample scenario.",
        )
        parser.add_argument(
            "--days", "-d",
            type=int,
            action="append",
            help="Day frame to evaluate (repeatable). Defaults to 3, 7, 10.",
        )
        parser.add_argument(
            "--reply", "-r",
            default=DEFAULT_DUMMY_REPLY,
            help="The synthetic prospect reply to inject (defaults to a built-in one).",
        )
        parser.add_argument(
            "--output", "-o",
            default=None,
            help="Path to write the comparison report.",
        )

    def handle(self, *args, **options):
        scenario_path = options["scenario"]
        day_frames = options["days"] or DEFAULT_DAY_FRAMES
        reply = options["reply"]
        output_path = options["output"]

        if scenario_path:
            scenario = load_scenario(scenario_path)
        else:
            scenario = default_scenario()

        run_reply_sweep(
            scenario,
            day_frames=day_frames,
            dummy_reply=reply,
            output_path=output_path,
        )
