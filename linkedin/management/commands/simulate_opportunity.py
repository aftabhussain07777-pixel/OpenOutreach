# linkedin/management/commands/simulate_opportunity.py
"""Management command to test the opportunity detector pipeline.

Runs a scripted conversation with opportunity signals, evaluates the
assessment, sends (or previews) the Telegram notification, flags the deal,
and verifies the follow-up agent is halted.

Usage:
    python manage.py simulate_opportunity
    python manage.py simulate_opportunity --no-send          # preview only
    python manage.py simulate_opportunity --reply "custom reply"
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from linkedin.simulation.scenario import default_scenario, load_scenario
from linkedin.simulation.opportunity_test import (
    DEFAULT_LATEST_REPLY,
    run_opportunity_test,
)


class Command(BaseCommand):
    help = (
        "Test the opportunity detector: assessment generation, notification "
        "trigger, and the follow-up agent halt on human takeover."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--scenario", "-s",
            default=None,
            help="Path to a scenario JSON file. Defaults to the built-in sample scenario.",
        )
        parser.add_argument(
            "--reply", "-r",
            default=DEFAULT_LATEST_REPLY,
            help="The latest prospect reply to assess (defaults to a built-in opportunity signal).",
        )
        parser.add_argument(
            "--no-send",
            action="store_true",
            help="Preview the notification without sending it via Telegram.",
        )

    def handle(self, *args, **options):
        scenario_path = options["scenario"]
        reply = options["reply"]
        send_notification = not options["no_send"]

        if scenario_path:
            scenario = load_scenario(scenario_path)
        else:
            scenario = default_scenario()

        run_opportunity_test(
            scenario,
            latest_reply=reply,
            send_notification=send_notification,
        )
