# linkedin/tasks/connect.py
"""Connect task — pulls one candidate, connects, self-reschedules.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from django.utils import timezone
from termcolor import colored

from linkedin.conf import CAMPAIGN_CONFIG
from linkedin.db.deals import increment_connect_attempts, set_profile_state
from linkedin.db.leads import disqualify_lead
from linkedin.models import ActionLog
from linkedin.enums import ProfileState
from linkedin.exceptions import ProfileInaccessibleError, ReachedConnectionLimit, SkipProfile

logger = logging.getLogger(__name__)

MAX_CONNECT_ATTEMPTS = 3


@dataclass
class ConnectStrategy:
    find_candidate: Callable
    delay: float
    qualifier: object


def strategy_for(campaign, qualifiers):
    """Build the right ConnectStrategy based on campaign type."""
    from linkedin.pipeline.pools import find_candidate

    qualifier = qualifiers.get(campaign.pk)

    return ConnectStrategy(
        find_candidate=lambda s: find_candidate(s, qualifier),
        delay=CAMPAIGN_CONFIG["connect_delay_seconds"],
        qualifier=qualifier,
    )


def handle_connect(task, session, qualifiers):
    from linkedin.actions.connect import send_connection_request
    from linkedin.actions.status import get_connection_status
    from linkedin.tasks.scheduler import enqueue_connect, seconds_until_tomorrow

    cfg = CAMPAIGN_CONFIG
    campaign = session.campaign
    campaign_id = campaign.pk
    strategy = strategy_for(campaign, qualifiers)

    def _reschedule():
        enqueue_connect(campaign_id, delay_seconds=strategy.delay)

    # --- Rate limit check ---
    if not session.linkedin_profile.can_execute(ActionLog.ActionType.CONNECT):
        enqueue_connect(campaign_id, delay_seconds=seconds_until_tomorrow())
        return

    # --- Get candidate ---
    candidate = strategy.find_candidate(session)
    if candidate is None:
        enqueue_connect(campaign_id, delay_seconds=cfg["connect_no_candidate_delay_seconds"])
        return

    public_id = candidate["public_identifier"]
    profile = candidate.get("profile") or candidate

    from crm.models import Deal

    deal = Deal.objects.filter(
        lead__public_identifier=public_id,
        campaign=session.campaign,
    ).first()
    reason = deal.reason if deal else ""
    stats = strategy.qualifier.explain(candidate, session) if strategy.qualifier else ""
    logger.info("[%s] %s", campaign, colored("\u25b6 connect", "cyan", attrs=["bold"]))
    logger.info("[%s] %s (%s) — %s", campaign, public_id, stats, reason or "")

    try:
        status = get_connection_status(session, profile)

        if status in (ProfileState.CONNECTED, ProfileState.PENDING):
            # set_profile_state triggers the scheduler hook, which enqueues
            # follow_up (CONNECTED) or check_pending (PENDING).
            set_profile_state(session, public_id, status.value)
            _reschedule()
            return

        # get_connection_status already navigated to the profile page
        new_state = send_connection_request(session=session, profile=profile)

        if new_state == ProfileState.QUALIFIED:
            # No Connect button found — track attempt, disqualify after MAX_CONNECT_ATTEMPTS
            attempts = increment_connect_attempts(session, public_id)
            if attempts >= MAX_CONNECT_ATTEMPTS:
                reason = f"Unreachable: no Connect button after {attempts} attempts"
                disqualify_lead(public_id)
                set_profile_state(session, public_id, ProfileState.FAILED.value, reason=reason)
                logger.warning("Disqualified %s — %s", public_id, reason)
            else:
                set_profile_state(session, public_id, new_state.value)
                logger.debug("%s: connect attempt %d/%d — no button found", public_id, attempts, MAX_CONNECT_ATTEMPTS)
        else:
            set_profile_state(session, public_id, new_state.value)
            session.linkedin_profile.record_action(
                ActionLog.ActionType.CONNECT, session.campaign,
            )

    except ReachedConnectionLimit as e:
        logger.warning("Rate limited: %s", e)
        session.linkedin_profile.mark_exhausted(ActionLog.ActionType.CONNECT)
        enqueue_connect(campaign_id, delay_seconds=seconds_until_tomorrow())
        return
    except ProfileInaccessibleError as e:
        logger.warning("Profile inaccessible — marking FAILED: %s", e)
        set_profile_state(session, public_id, ProfileState.FAILED.value,
                          reason=f"Profile inaccessible: {e}")
    except SkipProfile as e:
        logger.warning("Skipping %s: %s", public_id, e)
        set_profile_state(session, public_id, ProfileState.FAILED.value)

    _reschedule()


