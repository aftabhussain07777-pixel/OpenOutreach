# linkedin/onboarding.py
"""Onboarding: create Campaign + LinkedInProfile + LLM config in DB.

Two ways to supply config:
- OnboardConfig.from_json(path) — from a JSON file (non-interactive / cloud).
- collect_from_wizard()         — interactive questionary wizard (needs TTY).

Both return an OnboardConfig; ``apply()`` is the single write path.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass

from linkedin.conf import (
    DEFAULT_CONNECT_DAILY_LIMIT,
    DEFAULT_FOLLOW_UP_DAILY_LIMIT,
    ROOT_DIR,
)

DEFAULT_PRODUCT_DOCS = ROOT_DIR / "README.md"
DEFAULT_CAMPAIGN_OBJECTIVE = ROOT_DIR / "docs" / "default_campaign.md"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config dataclass (pure data — no I/O)
# ---------------------------------------------------------------------------


@dataclass
class OnboardConfig:
    """All values needed to onboard — filled interactively or from JSON."""

    linkedin_email: str = ""
    linkedin_password: str = ""
    campaign_name: str = ""
    product_description: str = ""
    campaign_objective: str = ""
    booking_link: str = ""
    seed_urls: str = ""
    llm_provider: str = "openai"
    llm_api_key: str = ""
    ai_model: str = ""
    llm_api_base: str = ""
    newsletter: bool = True
    connect_daily_limit: int = DEFAULT_CONNECT_DAILY_LIMIT
    follow_up_daily_limit: int = DEFAULT_FOLLOW_UP_DAILY_LIMIT
    legal_acceptance: bool = False


# ---------------------------------------------------------------------------
# State inspection
# ---------------------------------------------------------------------------

_CAMPAIGN_KEYS = {
    "campaign_name",
    "product_description",
    "campaign_objective",
    "booking_link",
    "seed_urls",
}
_ACCOUNT_KEYS = {
    "linkedin_email",
    "linkedin_password",
    "newsletter",
    "connect_daily_limit",
    "follow_up_daily_limit",
    "legal_acceptance",
}
_LLM_KEYS = {"llm_provider", "llm_api_key", "ai_model", "llm_api_base"}
_ALL_KEYS = _CAMPAIGN_KEYS | _ACCOUNT_KEYS | _LLM_KEYS


def missing_keys() -> set[str]:
    """Return onboarding field keys that still need values."""
    from linkedin.models import Campaign, LinkedInProfile, SiteConfig

    keys: set[str] = set()

    if not Campaign.objects.exists():
        keys |= _CAMPAIGN_KEYS

    if not LinkedInProfile.objects.filter(active=True).exists():
        keys |= _ACCOUNT_KEYS

    cfg = SiteConfig.load()
    if not cfg.llm_provider:
        keys.add("llm_provider")
    if not cfg.llm_api_key:
        keys.add("llm_api_key")
    if not cfg.ai_model:
        keys.add("ai_model")
    # llm_api_base is only required for the openai_compatible provider.
    if (
        cfg.llm_provider == SiteConfig.LLMProvider.OPENAI_COMPATIBLE
        and not cfg.llm_api_base
    ):
        keys.add("llm_api_base")

    return keys


# ---------------------------------------------------------------------------
# Interactive collection (needs TTY)
# ---------------------------------------------------------------------------


def collect_from_wizard() -> OnboardConfig:
    """Run the questionary wizard for missing fields; return an OnboardConfig.

    Raises SystemExit if the user cancels.
    """
    import questionary
    from questionary import Choice

    missing = missing_keys()
    if not missing:
        return OnboardConfig()

    questions: list = []

    # Group: LinkedIn account
    if "linkedin_email" in missing:
        questions.append(
            questionary.text("LinkedIn email / username:", name="linkedin_email")
        )
    if "linkedin_password" in missing:
        questions.append(
            questionary.password("LinkedIn password:", name="linkedin_password")
        )

    # Group: Campaign
    if "campaign_name" in missing:
        questions.append(
            questionary.text(
                "Campaign name (e.g. 'B2B SaaS Founders'):", name="campaign_name"
            )
        )
    if "product_description" in missing:
        questions.append(
            questionary.text(
                "Product / service description (what you sell, who it's for):",
                name="product_description",
            )
        )
    if "campaign_objective" in missing:
        questions.append(
            questionary.text(
                "Campaign objective (what kind of prospect you're looking for):",
                name="campaign_objective",
            )
        )
    if "booking_link" in missing:
        questions.append(
            questionary.text(
                "Booking / Calendly link (optional):",
                name="booking_link",
                default="",
            )
        )
    if "seed_urls" in missing:
        questions.append(
            questionary.text(
                "Seed LinkedIn profile URLs (comma-separated, optional):",
                name="seed_urls",
                default="",
            )
        )

    # Group: LLM
    if "llm_provider" in missing:
        questions.append(
            questionary.select(
                "LLM provider:",
                choices=[
                    Choice("OpenAI", "openai"),
                    Choice("Anthropic", "anthropic"),
                    Choice("Google", "google"),
                    Choice("Groq", "groq"),
                    Choice("Mistral", "mistral"),
                    Choice("Cohere", "cohere"),
                    Choice("OpenAI-compatible", "openai_compatible"),
                ],
                name="llm_provider",
            )
        )
    if "llm_api_key" in missing:
        questions.append(questionary.password("LLM API key:", name="llm_api_key"))
    if "ai_model" in missing:
        questions.append(
            questionary.text(
                "AI model (e.g. 'gpt-4o', 'claude-sonnet-4-20250514'):", name="ai_model"
            )
        )
    if "llm_api_base" in missing:
        questions.append(
            questionary.text(
                "LLM API base URL (only for openai_compatible):",
                name="llm_api_base",
                default="",
            )
        )

    # Group: Account settings
    if "newsletter" in missing:
        questions.append(
            questionary.confirm(
                "Subscribe to product updates?", default=True, name="newsletter"
            )
        )
    if "connect_daily_limit" in missing:
        questions.append(
            questionary.text(
                "Daily connect request limit:",
                default=str(DEFAULT_CONNECT_DAILY_LIMIT),
                name="connect_daily_limit",
            )
        )
    if "follow_up_daily_limit" in missing:
        questions.append(
            questionary.text(
                "Daily follow-up message limit:",
                default=str(DEFAULT_FOLLOW_UP_DAILY_LIMIT),
                name="follow_up_daily_limit",
            )
        )

    # Legal acceptance — always required when onboarding
    if "legal_acceptance" in missing:
        questions.append(
            questionary.confirm(
                "I accept the legal terms and conditions of using LinkedIn automation.",
                default=False,
                name="legal_acceptance",
            )
        )

    if not questions:
        return OnboardConfig()

    form = questionary.unsafe_prompt(questions)
    if form is None:
        raise SystemExit("Onboarding cancelled.")

    # Cast numeric fields
    answers = dict(form)
    if "connect_daily_limit" in answers:
        answers["connect_daily_limit"] = int(answers["connect_daily_limit"])
    if "follow_up_daily_limit" in answers:
        answers["follow_up_daily_limit"] = int(answers["follow_up_daily_limit"])

    return OnboardConfig(
        **{k: v for k, v in answers.items() if k in OnboardConfig.__dataclass_fields__}
    )


# ---------------------------------------------------------------------------
# Record creation (pure DB, no I/O)
# ---------------------------------------------------------------------------


def _read_default_file(path) -> str:
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


def _create_campaign(
    name: str, product_docs: str, objective: str, booking_link: str = ""
):
    """Create a Campaign record and return it."""
    from linkedin.models import Campaign

    campaign = Campaign.objects.create(
        name=name,
        product_docs=product_docs,
        campaign_objective=objective,
        booking_link=booking_link,
    )
    logger.info("Campaign '%s' created!", name)
    return campaign


def _create_account(
    campaign,
    email: str,
    password: str,
    *,
    subscribe: bool = True,
    connect_daily: int = DEFAULT_CONNECT_DAILY_LIMIT,
    follow_up_daily: int = DEFAULT_FOLLOW_UP_DAILY_LIMIT,
):
    """Create a User + LinkedInProfile record and return the profile."""
    from django.contrib.auth.models import User

    from linkedin.models import LinkedInProfile

    handle = email.split("@")[0].lower().replace(".", "_").replace("+", "_")

    user, created = User.objects.get_or_create(
        username=handle,
        defaults={"is_staff": True, "is_active": True},
    )
    if created:
        user.set_unusable_password()
        user.save()

    campaign.users.add(user)

    profile = LinkedInProfile.objects.create(
        user=user,
        linkedin_username=email,
        linkedin_password=password,
        subscribe_newsletter=subscribe,
        connect_daily_limit=connect_daily,
        follow_up_daily_limit=follow_up_daily,
    )

    logger.info("Account '%s' created! (email=%s)", handle, email)
    return profile


def _create_seed_leads(campaign, seed_urls: str) -> None:
    """Parse seed URL text and create QUALIFIED leads."""
    if not seed_urls or not seed_urls.strip():
        return
    from linkedin.setup.seeds import create_seed_leads, parse_seed_urls

    public_ids = parse_seed_urls(seed_urls)
    if public_ids:
        created = create_seed_leads(campaign, public_ids)
        logger.info("%d seed profile(s) added as QUALIFIED.", created)


# ---------------------------------------------------------------------------
# Single write path
# ---------------------------------------------------------------------------


def apply(config: OnboardConfig) -> None:
    """Idempotent: create missing Campaign, Account, env vars, and legal acceptance."""
    from linkedin.management.setup_crm import DEFAULT_CAMPAIGN_NAME
    from linkedin.models import Campaign, LinkedInProfile

    # Campaign
    campaign = Campaign.objects.first()
    if campaign is None and config.campaign_name:
        campaign = _create_campaign(
            name=config.campaign_name or DEFAULT_CAMPAIGN_NAME,
            product_docs=config.product_description
            or _read_default_file(DEFAULT_PRODUCT_DOCS),
            objective=config.campaign_objective
            or _read_default_file(DEFAULT_CAMPAIGN_OBJECTIVE),
            booking_link=config.booking_link,
        )
        _create_seed_leads(campaign, config.seed_urls)

    # Account
    if (
        not LinkedInProfile.objects.filter(active=True).exists()
        and config.linkedin_email
    ):
        _create_account(
            campaign,
            config.linkedin_email,
            config.linkedin_password,
            subscribe=config.newsletter,
            connect_daily=config.connect_daily_limit,
            follow_up_daily=config.follow_up_daily_limit,
        )

    # LLM config → DB
    from linkedin.models import SiteConfig

    cfg = SiteConfig.load()
    updated = False
    for field, val in [
        ("llm_provider", config.llm_provider),
        ("llm_api_key", config.llm_api_key),
        ("ai_model", config.ai_model),
        ("llm_api_base", config.llm_api_base),
    ]:
        if val:
            setattr(cfg, field, val)
            updated = True
    if updated:
        cfg.save()
        logger.info("LLM config saved to database.")

    # Legal
    if config.legal_acceptance:
        from linkedin.models import LinkedInProfile as LP

        LP.objects.filter(legal_accepted=False, active=True).update(legal_accepted=True)
