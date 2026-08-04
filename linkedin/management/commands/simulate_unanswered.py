# linkedin/management/commands/simulate_unanswered.py
"""Management command to run the unanswered follow-up sweep.

Tests what the follow-up agent generates when the prospect never replies
to the initial message, across multiple time frames.

Usage:
    python manage.py simulate_unanswered
    python manage.py simulate_unanswered --scenario ./my_scenario.json
    python manage.py simulate_unanswered --days 3 7 10 14
    python manage.py simulate_unanswered --days 3 --days 5
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from linkedin.simulation.scenario import default_scenario, load_scenario
from linkedin.simulation.unanswered_sweep import DEFAULT_DAY_FRAMES, run_unanswered_sweep


class Command(BaseCommand):
    help = (
        "Run the unanswered follow-up sweep — what the agent generates when "
        "the prospect never replies, at 3/7/10 days."
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
            "--output", "-o",
            default=None,
            help="Path to write the comparison report.",
        )

    def handle(self, *args, **options):
        scenario_path = options["scenario"]
        day_frames = options["days"] or DEFAULT_DAY_FRAMES
        output_path = options["output"]

        if scenario_path:
            scenario = load_scenario(scenario_path)
        else:
            scenario = default_scenario()

        run_unanswered_sweep(
            scenario,
            day_frames=day_frames,
            output_path=output_path,
        )
