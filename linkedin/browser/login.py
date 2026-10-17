# linkedin/browser/login.py
import logging
import shutil
from urllib.parse import unquote, urlsplit

from patchright.sync_api import TimeoutError as PlaywrightTimeoutError
from patchright.sync_api import sync_playwright
from termcolor import colored

from linkedin.browser.nav import goto_page, human_type, resolve_locator
from linkedin.conf import (
    ACTIVE_TIMEZONE,
    BROWSER_BLOCKED_RESOURCE_TYPES,
    BROWSER_DEFAULT_TIMEOUT_MS,
    BROWSER_LOCALE,
    BROWSER_LOGIN_TIMEOUT_MS,
    BROWSER_PROFILE_DIR,
    BROWSER_SLOW_MO,
    BROWSER_TIMEZONE_ID,
    EXPECTED_EXIT_IP,
    PROXY_BYPASS,
    PROXY_URL,
)

logger = logging.getLogger(__name__)

LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"

EMAIL_LOCATORS = [
    lambda p: p.get_by_role("textbox", name="Email or phone"),
    lambda p: p.get_by_label("Email or phone"),
    lambda p: p.locator('input[autocomplete="webauthn"]'),
    lambda p: p.locator('input[name="session_key"]'),
    lambda p: p.locator('input#username'),
    lambda p: p.locator('form input[type="text"]'),
]

PASSWORD_LOCATORS = [
    lambda p: p.locator('input[type="password"]'),
    lambda p: p.locator('input[autocomplete="current-password"]'),
    lambda p: p.get_by_role("textbox", name="Password"),
    lambda p: p.get_by_label("Password"),
    lambda p: p.locator('input[name="session_password"]'),
    lambda p: p.locator('input#password'),
]

SUBMIT_LOCATORS = [
    lambda p: p.locator("form").get_by_role("button", name="Sign in", exact=True),
    lambda p: p.get_by_role("button", name="Sign in", exact=True),
    lambda p: p.locator('form button[type="submit"]'),
    lambda p: p.locator('button[type="submit"]'),
]

COMPLY_LOCATORS = [
    lambda p: p.locator('button#content__button--primary--muted'),
    lambda p: p.get_by_role("button", name="Agree to comply", exact=True),
    lambda p: p.locator('button.content__button--primary'),
]

COMPLY_PROBE_TIMEOUT_MS = 5000


def dismiss_comply_gate(page, timeout_ms: int = COMPLY_PROBE_TIMEOUT_MS) -> bool:
    """Click LinkedIn's 'Agree to comply' interstitial if present. Return True if clicked."""
    for factory in COMPLY_LOCATORS:
        locator = factory(page).first
        try:
            locator.wait_for(state="visible", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            continue
        logger.info(colored("Dismissing 'Agree to comply' interstitial", "yellow"))
        locator.click()
        return True
    return False


def playwright_login(session: "AccountSession"):
    page = session.page
    lp = session.linkedin_profile
    logger.info(colored("Fresh login sequence starting", "cyan") + f" for {session}")

    goto_page(
        session,
        action=lambda: page.goto(LINKEDIN_LOGIN_URL),
        expected_url_pattern="/login",
        error_message="Failed to load login page",
    )

    human_type(resolve_locator(page, EMAIL_LOCATORS), lp.linkedin_username)
    session.wait()
    human_type(resolve_locator(page, PASSWORD_LOCATORS), lp.linkedin_password)
    session.wait()

    submit = resolve_locator(page, SUBMIT_LOCATORS)
    submit.click()
    dismiss_comply_gate(page)
    goto_page(
        session,
        action=lambda: None,
        expected_url_pattern="/feed",
        timeout=BROWSER_LOGIN_TIMEOUT_MS,
        error_message="Login failed – no redirect to feed",
    )


def _redacted_proxy(server: str) -> str:
    """Proxy URL with credentials masked for logs (never print the password)."""
    try:
        scheme, rest = server.split("://", 1)
        if "@" in rest:
            _creds, host = rest.rsplit("@", 1)
            return f"{scheme}://***@{host}"
        return server
    except ValueError:
        return "***"


def _proxy_config() -> dict | None:
    """Playwright ``proxy`` dict from env, or None for direct connection.

    Credentials are passed as separate ``username``/``password`` fields —
    Chromium ignores credentials embedded in the ``server`` URL and fails
    with ``ERR_INVALID_AUTH_CREDENTIALS`` on the proxy's 407 challenge.
    """
    if not PROXY_URL:
        return None
    parts = urlsplit(PROXY_URL)
    if not parts.hostname:
        parts = urlsplit("http://" + PROXY_URL)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    proxy: dict = {"server": server}
    if parts.username:
        proxy["username"] = unquote(parts.username)
        proxy["password"] = unquote(parts.password or "")
    if PROXY_BYPASS:
        proxy["bypass"] = PROXY_BYPASS
    return proxy


def _launch_options() -> dict:
    """Pinned persistent-context options: proxy + stable fingerprint.

    Patchright best practice: real Google Chrome, persistent profile,
    no user-supplied user-agent or viewport (``no_viewport`` follows the
    actual Xvfb window).
    """
    options: dict = {
        "user_data_dir": str(BROWSER_PROFILE_DIR),
        "channel": "chrome",
        "headless": False,
        "no_viewport": True,
        "slow_mo": BROWSER_SLOW_MO,
        "locale": BROWSER_LOCALE,
        "timezone_id": BROWSER_TIMEZONE_ID or ACTIVE_TIMEZONE,
    }
    proxy = _proxy_config()
    if proxy:
        options["proxy"] = proxy
    return options


def log_exit_ip(page) -> None:
    """Fetch and log the live egress IP of the browser context.

    Uses a throwaway page: LinkedIn's CSP blocks in-page ``fetch()`` to
    third-party hosts. Always logged so IP changes can be correlated
    with LinkedIn challenges. When ``EXPECTED_EXIT_IP`` is set
    (static-IP instances), a mismatch — or an unverifiable IP — is a
    hard failure.
    """
    probe = None
    try:
        probe = page.context.new_page()
        probe.goto("https://api.ipify.org", wait_until="domcontentloaded")
        actual = probe.inner_text("body").strip()
    except Exception as e:
        if EXPECTED_EXIT_IP:
            raise RuntimeError(
                f"Exit-IP check failed (expected {EXPECTED_EXIT_IP}): {e}"
            ) from e
        logger.warning("Could not determine exit IP: %s", e)
        return
    finally:
        if probe:
            probe.close()
    logger.info("Exit IP: %s", actual)
    if EXPECTED_EXIT_IP and actual != EXPECTED_EXIT_IP:
        raise RuntimeError(
            f"Exit-IP mismatch: expected {EXPECTED_EXIT_IP}, got {actual}. "
            "Refusing to run the account on the wrong IP."
        )


_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")

_COOKIE_FIELDS = frozenset({
    "name", "value", "url", "domain", "path",
    "expires", "httpOnly", "secure", "sameSite",
})


def _prepare_profile_dir() -> None:
    """Create the persistent profile dir and clear stale singleton locks."""
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    for name in _SINGLETON_FILES:
        (BROWSER_PROFILE_DIR / name).unlink(missing_ok=True)


def reset_browser_profile() -> None:
    """Delete the persistent Chrome profile (forces a fresh login)."""
    shutil.rmtree(BROWSER_PROFILE_DIR, ignore_errors=True)


def _has_auth_cookie(context) -> bool:
    return any(
        c.get("name") == "li_at" and c.get("value")
        for c in context.cookies()
    )


def _seed_cookies(context, storage_state: dict) -> None:
    """Copy DB-persisted cookies into a profile without a LinkedIn session."""
    if _has_auth_cookie(context):
        return
    cookies = [
        {k: v for k, v in cookie.items() if k in _COOKIE_FIELDS}
        for cookie in storage_state.get("cookies") or []
    ]
    if cookies:
        context.add_cookies(cookies)
        logger.info("Seeded %d saved cookies into the profile", len(cookies))


def launch_browser(storage_state=None):
    logger.debug("Launching Patchright")
    _prepare_profile_dir()
    playwright = sync_playwright().start()
    options = _launch_options()
    if "proxy" in options:
        logger.info(
            "Routing browser through proxy %s (tz=%s locale=%s)",
            _redacted_proxy(PROXY_URL),
            options["timezone_id"],
            options["locale"],
        )
    context = playwright.chromium.launch_persistent_context(**options)
    if storage_state:
        _seed_cookies(context, storage_state)
    if BROWSER_BLOCKED_RESOURCE_TYPES:
        context.route("**/*", _block_heavy_resources)
    context.set_default_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
    context.set_default_navigation_timeout(BROWSER_DEFAULT_TIMEOUT_MS)
    page = context.pages[0] if context.pages else context.new_page()
    return page, context, context.browser, playwright


def _block_heavy_resources(route):
    """Abort configured resource types (default image/media) — DOM + Voyager
    XHRs are unaffected. Disabled entirely when
    BROWSER_BLOCKED_RESOURCE_TYPES is empty (manual/CAPTCHA sessions)."""
    if route.request.resource_type in BROWSER_BLOCKED_RESOURCE_TYPES:
        route.abort()
    else:
        route.continue_()


def _save_cookies(session):
    """Persist Playwright storage state (cookies) to the DB."""
    state = session.context.storage_state()
    session.linkedin_profile.cookie_data = state
    session.linkedin_profile.save(update_fields=["cookie_data"])


def start_browser_session(session: "AccountSession"):
    logger.debug("Configuring browser for %s", session)

    session.linkedin_profile.refresh_from_db(fields=["cookie_data"])
    cookie_data = session.linkedin_profile.cookie_data
    storage_state = cookie_data if cookie_data else None

    session.page, session.context, session.browser, session.playwright = launch_browser(storage_state=storage_state)

    if _has_auth_cookie(session.context):
        logger.info("Resuming saved session for %s", session)
        session.page.goto(LINKEDIN_FEED_URL)
        dismiss_comply_gate(session.page)
        goto_page(
            session,
            action=lambda: None,
            expected_url_pattern="/feed",
            timeout=BROWSER_DEFAULT_TIMEOUT_MS,
            error_message="Saved session invalid",
        )
    else:
        playwright_login(session)
        _save_cookies(session)
        logger.info(colored("Login successful – session saved", "green", attrs=["bold"]))

    # "domcontentloaded" — "load" waits for every subresource (analytics
    # beacons, lazy media) and on LinkedIn that event may never fire,
    # hanging the daemon for the duration of the BROWSER_DEFAULT_TIMEOUT.
    session.page.wait_for_load_state("domcontentloaded")
    log_exit_ip(session.page)
    logger.info(colored("Browser ready", "green", attrs=["bold"]))


if __name__ == "__main__":
    from linkedin.browser.registry import cli_parser, cli_session

    parser = cli_parser("Start a LinkedIn browser session")
    args = parser.parse_args()
    session = cli_session(args)
    session.ensure_browser()

    start_browser_session(session=session)
    logger.info("Logged in! Close browser manually.")
    session.page.pause()
