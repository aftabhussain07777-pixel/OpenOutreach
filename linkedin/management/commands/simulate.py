# linkedin/management/commands/simulate.py
"""Management command to run a conversation simulation.

Usage:
    python manage.py simulate
    python manage.py simulate --scenario linkedin/simulation/sample_scenario.json
    python manage.py simulate --scenario ./my_scenario.json --max-turns 5
    python manage.py simulate --scenario ./my_scenario.json --transcript /tmp/out.txt
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from linkedin.simulation.scenario import default_scenario, load_scenario
from linkedin.simulation.simulation_runner import run_simulation


class Command(BaseCommand):
    help = "Run an offline simulation to stress-test the Conversation Agent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--scenario", "-s",
            default=None,
            help="Path to a scenario JSON file. Defaults to the built-in sample scenario.",
        )
        parser.add_argument(
            "--max-turns", "-n",
            type=int,
            default=10,
            help="Maximum conversation turns (default: 10).",
        )
        parser.add_argument(
            "--transcript", "-t",
            default=None,
            help="Path to write the transcript. Defaults to linkedin/simulation/transcripts/.",
        )

    def handle(self, *args, **options):
        scenario_path = options["scenario"]
        max_turns = options["max_turns"]
        transcript_path = options["transcript"]

        if scenario_path:
            scenario = load_scenario(scenario_path)
        else:
            scenario = default_scenario()

        run_simulation(
            scenario,
            max_turns=max_turns,
            transcript_path=transcript_path,
        )
