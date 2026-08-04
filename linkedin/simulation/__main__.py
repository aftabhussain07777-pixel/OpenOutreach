# linkedin/simulation/__main__.py
"""CLI entry point for running conversation simulations.

Bootstraps Django so production agent code can be imported,
then delegates to the simulation runner.

Usage:
    python -m linkedin.simulation [--scenario <path>] [--max-turns 10]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# Bootstrap Django before importing anything that touches models
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "linkedin.django_settings")
import django  # noqa: E402
django.setup()

from linkedin.simulation.scenario import default_scenario, load_scenario  # noqa: E402
from linkedin.simulation.simulation_runner import run_simulation  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a simulated LinkedIn conversation for stress-testing.",
    )
    parser.add_argument(
        "--scenario", "-s",
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
        help="Path to write the transcript. Defaults to linkedin/simulation/transcripts/.",
    )
    args = parser.parse_args()

    if args.scenario:
        scenario = load_scenario(args.scenario)
    else:
        scenario = default_scenario()

    print(f"Running simulation: {scenario.name}")
    print(f"Max turns: {args.max_turns}")
    print()

    transcript = run_simulation(
        scenario,
        max_turns=args.max_turns,
        transcript_path=args.transcript,
    )

    print(f"\nSimulation finished: {transcript.total_turns} turns")
    print(f"Ended early: {transcript.ended_early}")
    if transcript.turns:
        last = transcript.turns[-1]
        print(f"Final action: {last.agent_action}")
        if last.agent_outcome:
            print(f"Outcome: {last.agent_outcome}")


if __name__ == "__main__":
    main()
