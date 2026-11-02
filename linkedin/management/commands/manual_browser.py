import json
import logging
import signal

from django.core.management.base import BaseCommand

from linkedin.browser.login import launch_browser, log_exit_ip
from linkedin.logging import configure_logging

LINKEDIN_SIGNUP_URL = "https://www.linkedin.com/signup"


class Command(BaseCommand):
    help = (
        "Launch a browser (proxy + pinned fingerprint) for manual work over VNC "
        "— account signup, warmup browsing. Touches no DB state."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            default=LINKEDIN_SIGNUP_URL,
            help=f"Page to open (default: {LINKEDIN_SIGNUP_URL}).",
        )
        parser.add_argument(
            "--save-state",
            default="",
            help="Write Playwright storage_state JSON (cookies) here on exit.",
        )
        parser.add_argument(
            "--load-state",
            default="",
            help="Seed cookies into a fresh profile from a Playwright storage_state JSON file.",
        )

    def handle(self, *args, **options):
        save_state = options["save_state"]
        load_state = options["load_state"]
        configure_logging(level=logging.INFO)

        storage_state = None
        if load_state:
            with open(load_state, encoding="utf-8") as fh:
                storage_state = json.load(fh)
            cookies = storage_state.get("cookies") or []
            has_li_at = any(c.get("name") == "li_at" and c.get("value") for c in cookies)
            self.stdout.write(
                f"Load-state: {len(cookies)} cookies, "
                f"li_at {'present' if has_li_at else 'MISSING'}"
            )
            if not has_li_at:
                self.stdout.write(self.style.WARNING("No li_at — expect a fresh login."))

        page, context, browser, playwright = launch_browser(storage_state=storage_state)
        try:
            page.goto(options["url"], wait_until="domcontentloaded")
            log_exit_ip(page)

            self.stdout.write(self.style.SUCCESS("Manual browser ready."))
            self.stdout.write("Interact with it over VNC. Stop the container to close.")
            if save_state:
                self.stdout.write(f"Cookies will be saved to {save_state} on exit.")

            signal.signal(signal.SIGTERM, self._raise_interrupt)
            signal.signal(signal.SIGINT, self._raise_interrupt)
            try:
                while True:
                    page.wait_for_timeout(1000)
            except KeyboardInterrupt:
                self.stdout.write("Closing — saving state…")
        finally:
            if save_state and context:
                context.storage_state(path=save_state)
                self.stdout.write(self.style.SUCCESS(f"Storage state saved to {save_state}"))
            if context:
                context.close()
            if browser:
                browser.close()
            if playwright:
                playwright.stop()

    @staticmethod
    def _raise_interrupt(signum, frame):
        raise KeyboardInterrupt
