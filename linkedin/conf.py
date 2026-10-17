# linkedin/conf.py
from __future__ import annotations

import os
from pathlib import Path

from linkedin.tz_detect import system_timezone


# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
ROOT_DIR = Path(__file__).parent.parent

PROMPTS_DIR = Path(__file__).parent / "templates" / "prompts"

DIAGNOSTICS_DIR = Path("/tmp/openoutreach-diagnostics")

FASTEMBED_CACHE_DIR = ROOT_DIR / ".cache" / "fastembed"

FIXTURE_DIR = ROOT_DIR / "tests" / "fixtures"
FIXTURE_PROFILES_DIR = FIXTURE_DIR / "profiles"
FIXTURE_PAGES_DIR = FIXTURE_DIR / "pages"
DUMP_PAGES = False

# ----------------------------------------------------------------------
# Telegram notifications — general alerts (failures, task errors, etc.)
# Leave blank to disable.
# ----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8887475264:AAE7l58sNh-hKAPnIqGv9mR2h_k6Bq8y2j8"  # @outreach_alerts_4464_bot
TELEGRAM_CHAT_ID = "8901396168"

# ----------------------------------------------------------------------
# Telegram notifications — opportunity alerts only (human takeover)
# ----------------------------------------------------------------------
OPPORTUNITY_BOT_TOKEN = "8997123346:AAEw1PUiB5g0UXvNYW776bzQgEQHrxprQX4"  # @outreach_opportunity_bot
OPPORTUNITY_CHAT_ID = "8901396168"

MIN_DELAY = 5
MAX_DELAY = 8

# ----------------------------------------------------------------------
# Browser config
# ----------------------------------------------------------------------
BROWSER_SLOW_MO = 200
BROWSER_DEFAULT_TIMEOUT_MS = 30_000
BROWSER_LOGIN_TIMEOUT_MS = 40_000
BROWSER_NAV_TIMEOUT_MS = 10_000
# Comma-separated resource types to abort. Set BROWSER_BLOCKED_RESOURCE_TYPES
# to "" to load everything (required for CAPTCHA/signup over VNC — reCAPTCHA
# is drawn with images) or e.g. "media" to allow images but still block
# video/audio.
BROWSER_BLOCKED_RESOURCE_TYPES = frozenset(
    t.strip()
    for t in os.environ.get("BROWSER_BLOCKED_RESOURCE_TYPES", "image,media").split(",")
    if t.strip()
)
HUMAN_TYPE_MIN_DELAY_MS = 50
HUMAN_TYPE_MAX_DELAY_MS = 200

# ----------------------------------------------------------------------
# Per-instance egress proxy (residential IP per LinkedIn account).
# Set PROXY_URL to route the whole Chromium context through it, e.g.
#   PROXY_URL=http://user:pass@gw.dataimpulse.com:10000
# Empty = direct connection (local dev default). PROXY_BYPASS is an
# optional comma-separated list of hosts that skip the proxy.
# EXPECTED_EXIT_IP is an optional fail-fast guard: when set,
# start_browser_session() verifies the live exit IP and crashes on
# mismatch instead of running the account on the wrong IP.
# ----------------------------------------------------------------------
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
PROXY_BYPASS = os.environ.get("PROXY_BYPASS", "").strip()
EXPECTED_EXIT_IP = os.environ.get("EXPECTED_EXIT_IP", "").strip()

# ----------------------------------------------------------------------
# Browser fingerprint pinning (anti-detection).
# Every instance pins a stable fingerprint matching its proxy geo, so
# LinkedIn never sees TZ/locale drift between restarts. No custom
# user-agent or viewport injection (Patchright best practice) — the
# persistent Chrome profile follows the real 1920x1080 Xvfb window.
# BROWSER_TIMEZONE_ID defaults to "" (= follow ACTIVE_TIMEZONE);
# set it explicitly per instance, e.g. America/New_York.
# BROWSER_PROFILE_DIR is the persistent Chrome profile per account
# instance; keep it on the mounted data volume so the session (cookies,
# cache, history) survives container restarts.
# ----------------------------------------------------------------------
BROWSER_TIMEZONE_ID = os.environ.get("BROWSER_TIMEZONE_ID", "").strip()
BROWSER_LOCALE = os.environ.get("BROWSER_LOCALE", "en-US").strip() or "en-US"
BROWSER_PROFILE_DIR = Path(
    os.environ.get("BROWSER_PROFILE_DIR", "").strip()
    or (ROOT_DIR / "data" / "browser-profile")
)

# ----------------------------------------------------------------------
# Onboarding defaults (shown to user during interactive setup)
# ----------------------------------------------------------------------
DEFAULT_CONNECT_DAILY_LIMIT = 20
DEFAULT_FOLLOW_UP_DAILY_LIMIT = 25

# ----------------------------------------------------------------------
# Active-hours schedule (daemon pauses outside this window)
# Set to False to run 24/7. Working hours are a single contiguous window;
# weekends are no longer special-cased (humans use LinkedIn 7 days a week).
# ----------------------------------------------------------------------
ENABLE_ACTIVE_HOURS = True
ACTIVE_START_HOUR = 9   # inclusive, local time
ACTIVE_END_HOUR = 19    # exclusive, local time
ACTIVE_TIMEZONE = system_timezone()

# ----------------------------------------------------------------------
# Planner cap for check_pending: at most this many lazy slots per 24h
# planning window, regardless of how many PENDING deals are overdue.
# Overflow rolls into the next planning cycle.
# ----------------------------------------------------------------------
CHECK_PENDING_DAILY_CAP = 100

# ----------------------------------------------------------------------
# Check messages schedule: number of inbox scans per 24h window. With
# a 10-hour active window (9-19), 8 slots ≈ every 75 min during working
# hours. The new approach uses the Voyager conversations API to scan
# the inbox, so it's cheap enough to run frequently.
# ----------------------------------------------------------------------
CHECK_MESSAGES_SLOTS_PER_DAY = 8

# ----------------------------------------------------------------------
# Campaign config (timing + ML defaults — hardcoded, no YAML)
# ----------------------------------------------------------------------
CAMPAIGN_CONFIG = {
    "check_pending_recheck_after_hours": 24,
    "min_action_interval": 120,
    "qualification_n_mc_samples": 100,
    "min_ready_to_connect_prob": 0.9,
    "min_positive_pool_prob": 0.20,
    "embedding_model": "BAAI/bge-small-en-v1.5",
    "enrich_min_delay_seconds": 6,
    "enrich_max_delay_seconds": 10,
    "enrich_max_per_page": 10,
    "burst_min_seconds": 2700,   # 45 min
    "burst_max_seconds": 3900,   # 65 min
    "break_min_seconds": 600,    # 10 min
    "break_max_seconds": 1200,   # 20 min
}


