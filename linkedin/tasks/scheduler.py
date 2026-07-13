# linkedin/tasks/scheduler.py
"""Per-type 24h planner with Poisson-spaced lazy task slots.

The daemon's task queue is *lazy*: each row carries only ``task_type``,
``campaign_id``, and ``scheduled_at``. The handler resolves a concrete
target (lead/deal) at execution time via a single eligibility query.

This module is the only place that creates ``Task`` rows. The pipeline
moves forward in three layers:

1. **Per-type planner** — ``plan_connect_window``,
   ``plan_follow_up_window``, ``plan_check_pending_window``. Each one,
   when no PENDING task of its type exists for a campaign, computes the
   right slot count ``n`` for the next 24h, inserts one row that fires
   immediately, and Poisson-spaces the remaining ``n - 1`` rows across
   the working portion of the window. The leading immediate slot kills
   the cold-start ramp (without it the first action would sit ``T/n``
   away on average — ~72 min for a 20/day campaign).

2. **State-transition hook** — ``on_deal_state_entered(deal)`` only
   updates ``deal.next_check_pending_at`` for PENDING transitions. It
   does **not** insert any Task row. CONNECTED and other transitions
   are no-ops.

3. **Reconcile** — ``reconcile(session)``. Recovers stale RUNNING tasks
   and calls each planner per campaign. The daemon invokes it on startup
   and whenever the queue has no ready task.
"""

from __future__ import annotations

import datetime
import logging
import random
from datetime import datetime as Datetime
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from linkedin.conf import (
    ACTIVE_END_HOUR,
    ACTIVE_START_HOUR,
    ACTIVE_TIMEZONE,
    CAMPAIGN_CONFIG,
    CHECK_MESSAGES_SLOTS_PER_DAY,
    CHECK_PENDING_DAILY_CAP,
    ENABLE_ACTIVE_HOURS,
)
from linkedin.enums import ProfileState
from linkedin.models import Task

logger = logging.getLogger(__name__)


# ── Working-hours arithmetic ──────────────────────────────────────────


def _working_intervals(start, end) -> list[tuple]:
    """Return ``[(s, e), ...]`` UTC datetimes for the working portions of
    ``[start, end]``. When ``ENABLE_ACTIVE_HOURS`` is False the only
    interval is ``[(start, end)]``."""
    if not ENABLE_ACTIVE_HOURS:
        return [(start, end)]

    tz = ZoneInfo(ACTIVE_TIMEZONE)
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)

    intervals: list[tuple] = []
    day = local_start.date()
    last_day = local_end.date()
    while day <= last_day:
        day_active_start = Datetime(
            day.year,
            day.month,
            day.day,
            ACTIVE_START_HOUR,
            tzinfo=tz,
        )
        day_active_end = Datetime(
            day.year,
            day.month,
            day.day,
            ACTIVE_END_HOUR,
            tzinfo=tz,
        )
        s = max(day_active_start, local_start)
        e = min(day_active_end, local_end)
        if e > s:
            intervals.append((s, e))
        day = day + timedelta(days=1)
    return intervals


def working_seconds_in_window(start, end) -> float:
    """Sum of seconds inside ``[ACTIVE_START_HOUR, ACTIVE_END_HOUR]`` between
    ``start`` and ``end``. Returns ``(end - start).total_seconds()`` when
    active hours are disabled."""
    if not ENABLE_ACTIVE_HOURS:
        return max(0.0, (end - start).total_seconds())
    return sum((e - s).total_seconds() for s, e in _working_intervals(start, end))


def poisson_slot_times(now, n: int, horizon_hours: float = 24) -> list:
    """Return ``n`` strictly-increasing timestamps inside the working
    portion of ``[now, now + horizon_hours]``.

    Implementation: sample ``n`` uniform positions in ``[0, T)`` (working
    seconds) and sort. This is the order-statistic representation of a
    conditional Poisson process given ``n`` arrivals in the window —
    same distribution as exponential inter-arrival sampling, but
    guarantees exactly ``n`` slots without overshoot. Mean spacing in
    working time is ``T / (n + 1)``.
    """
    if n <= 0:
        return []

    end = now + timedelta(hours=horizon_hours)
    intervals = _working_intervals(now, end)
    total = sum((e - s).total_seconds() for s, e in intervals)
    if total <= 0:
        return []

    positions = sorted(random.uniform(0, total) for _ in range(n))

    times: list = []
    cursor_interval = 0
    cursor_offset = 0.0  # working-seconds consumed before the current interval
    for pos in positions:
        while cursor_interval < len(intervals):
            s, e = intervals[cursor_interval]
            dur = (e - s).total_seconds()
            if pos < cursor_offset + dur:
                times.append(s + timedelta(seconds=pos - cursor_offset))
                break
            cursor_offset += dur
            cursor_interval += 1
    return times


# ── Per-type planners ─────────────────────────────────────────────────


def _has_pending(task_type: "Task.TaskType", campaign_id: int) -> bool:
    return Task.objects.filter(
        task_type=task_type,
        status=Task.Status.PENDING,
        payload__campaign_id=campaign_id,
    ).exists()


def _create_lazy_slots(
    task_type: "Task.TaskType", campaign_id: int, times: list
) -> int:
    if not times:
        return 0
    Task.objects.bulk_create(
        [
            Task(
                task_type=task_type,
                scheduled_at=t,
                payload={"campaign_id": campaign_id},
            )
            for t in times
        ]
    )
    return len(times)


def _plan_slots(task_type: "Task.TaskType", campaign_id: int, n: int) -> int:
    """Schedule *n* lazy slots: one fires immediately, the remaining
    ``n - 1`` are Poisson-spaced across the next 24h working window.

    The leading immediate slot is intentional — without it the first
    action of a freshly-planned window would sit ``T/n`` away on average
    (the mean of a single ``Exp(n/T)`` draw). That cold-start ramp made
    `make run` feel dead for ~an hour on a 20/day campaign.
    """
    if n <= 0:
        return 0
    now = timezone.now()
    times = [now] + poisson_slot_times(now, n - 1)
    return _create_lazy_slots(task_type, campaign_id, times)


def plan_connect_window(session, campaign) -> int:
    """Plan the next 24h of connect slots for *campaign*. No-op when a
    PENDING connect task already exists for the campaign.

    Only the daily limit is consulted — LinkedIn's own weekly ceiling
    surfaces at the handler boundary via ``ReachedConnectionLimit``.
    """
    if _has_pending(Task.TaskType.CONNECT, campaign.pk):
        return 0

    profile = session.linkedin_profile
    n = max(0, profile.connect_daily_limit - profile._daily_count("connect"))

    created = _plan_slots(Task.TaskType.CONNECT, campaign.pk, n)
    if created:
        logger.info(
            "[%s] planned %d connect slots over next 24h — 1 fires now, "
            "%d Poisson-spaced (daily=%d)",
            campaign,
            created,
            max(0, created - 1),
            profile.connect_daily_limit,
        )
    return created




def plan_check_messages_window(session, campaign) -> int:
    """Plan the next 24h of check_messages slots for *campaign*. No-op when a
    PENDING check_messages task already exists for the campaign.

    Creates ``CHECK_MESSAGES_SLOTS_PER_DAY`` slots Poisson-spaced across the
    next 24h working window. Unlike connect/follow-up there is no per-slot
    rate-limit to exhaust, so the scan is cheap (single API call to list
    conversations, then only syncs conversations that belong to tracked leads).

    On the very first plan (no completed check_messages task for this
    campaign), an immediate slot is added so daemon start catches any
    missed messages right away.
    """
    if _has_pending(Task.TaskType.CHECK_MESSAGES, campaign.pk):
        return 0

    now = timezone.now()
    has_ever_run = Task.objects.filter(
        task_type=Task.TaskType.CHECK_MESSAGES,
        payload__campaign_id=campaign.pk,
        status=Task.Status.COMPLETED,
    ).exists()

    n = CHECK_MESSAGES_SLOTS_PER_DAY
    if has_ever_run:
        # Normal cadence: Poisson-space n slots across the next 24h
        times = poisson_slot_times(now, n=n)
    else:
        # First-ever plan: 1 immediate + n-1 Poisson-spaced
        times = [now] + poisson_slot_times(now, n=n - 1)

    created = _create_lazy_slots(Task.TaskType.CHECK_MESSAGES, campaign.pk, times)
    if created:
        logger.info(
            "[%s] planned %d check_messages slots over next 24h%s",
            campaign,
            created,
            " (fires now — first run)" if not has_ever_run else "",
        )
    return created


def plan_check_pending_window(session, campaign) -> int:
    """Plan the next 24h of check_pending slots for *campaign*. Slot count
    matches the PENDING deals whose backoff has expired (or expires
    within the horizon), capped by ``CHECK_PENDING_DAILY_CAP``.

    Unlike connect/follow-up, an **immediate** slot is only created when a
    deal is already past-due *now*. If all due deals are in the future
    (backoff not yet expired), all slots are Poisson-spaced — this prevents
    an infinite loop where the planner creates an immediate slot, the
    handler finds nothing due, and reconcile re-plans immediately.
    """
    from crm.models import Deal

    if _has_pending(Task.TaskType.CHECK_PENDING, campaign.pk):
        return 0

    now = timezone.now()
    horizon = now + timedelta(hours=24)

    # Separate counts: past-due (ready now) vs. future-due (will become ready).
    n_past_due = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=ProfileState.PENDING,
        next_check_pending_at__lte=now,
    ).count()
    n_future_due = Deal.objects.filter(
        campaign_id=campaign.pk,
        state=ProfileState.PENDING,
        next_check_pending_at__gt=now,
        next_check_pending_at__lte=horizon,
    ).count()

    n_total = n_past_due + n_future_due
    n = min(n_total, CHECK_PENDING_DAILY_CAP)
    if n <= 0:
        return 0

    # Always skip 1 immediate slot if no deal is past-due right now —
    # otherwise the slot fires, finds nothing, reconcile re-plans, loop.
    if n_past_due > 0:
        times = [now] + poisson_slot_times(now, n - 1)
    else:
        # All slots are in the future — Poisson-space all n across the window.
        times = poisson_slot_times(now, n)

    created = _create_lazy_slots(Task.TaskType.CHECK_PENDING, campaign.pk, times)
    if created:
        n_immediate = 1 if n_past_due > 0 else 0
        logger.info(
            "[%s] planned %d check_pending slots over next 24h — "
            "%d fires now, %d Poisson-spaced (due=%d, cap=%d)",
            campaign,
            created,
            n_immediate,
            created - n_immediate,
            n_total,
            CHECK_PENDING_DAILY_CAP,
        )
    return created


# ── Delay helpers ─────────────────────────────────────────────────────


def seconds_until_tomorrow() -> float:
    """Seconds until 00:00 local time — used for daily rate-limit waits."""
    now = timezone.now()
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    return (tomorrow - now).total_seconds()




# ── State-transition hook ─────────────────────────────────────────────


def on_deal_state_entered(deal) -> None:
    """PENDING: stamp ``deal.next_check_pending_at = now + backoff_hours``.
    CONNECTED: stamp ``deal.next_follow_up_at = now`` (triggers first outreach
    on the next check_messages scan). All other transitions are no-ops."""
    state = ProfileState(deal.state)
    now = timezone.now()

    if state == ProfileState.PENDING:
        backoff = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
        deal.next_check_pending_at = now + timedelta(hours=backoff)
        deal.save(update_fields=["next_check_pending_at"])
    elif state == ProfileState.CONNECTED:
        deal.next_follow_up_at = now
        deal.save(update_fields=["next_follow_up_at"])


# ── Reconciliation ────────────────────────────────────────────────────


def _recover_stale_running_tasks() -> int:
    """Reset RUNNING tasks to PENDING. RUNNING rows can only linger if the
    daemon crashed mid-task, so they are always stale at reconcile time."""
    count = Task.objects.filter(status=Task.Status.RUNNING).update(
        status=Task.Status.PENDING,
    )
    if count:
        logger.info("Recovered %d stale running tasks", count)
    return count


_PLANNERS = (
    plan_connect_window,
    plan_check_pending_window,
    plan_check_messages_window,
)


def reconcile(session) -> None:
    """Recover stale RUNNING tasks, then ensure every (campaign, task_type)
    whose pending queue is empty gets a fresh 24h plan. Runs on daemon
    startup and whenever the queue has no ready task."""
    _recover_stale_running_tasks()
    for campaign in session.campaigns:
        for planner in _PLANNERS:
            planner(session, campaign)

    pending_count = Task.objects.pending().count()
    logger.info("Task queue reconciled: %d pending tasks", pending_count)
