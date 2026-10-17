import signal

from django.core.management.base import BaseCommand

from linkedin.browser.login import launch_browser, log_exit_ip

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

    def handle(self, *args, **options):
        save_state = options["save_state"]

        page, context, browser, playwright = launch_browser()
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
