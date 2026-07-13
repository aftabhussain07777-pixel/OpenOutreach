# Revert: Unify follow_up into check_messages

Use this prompt in an AI code editor to undo the changes that merged
`follow_up` into `check_messages`. This reverts to the original separate
architecture where `FOLLOW_UP` was its own task type and planner.

## Steps

### 1. Restore `linkedin/tasks/follow_up.py`

Replace the current file with the backup at project root:

```python
# Read the content of BACKUP_follow_up.py and write it to linkedin/tasks/follow_up.py
```

### 2. Restore `linkedin/tasks/scheduler.py`

- Re-add `plan_follow_up_window` function (see the original in `BACKUP_follow_up.py` header comments for the general shape, or reconstruct from the revert pattern below).
- Re-add `enqueue_follow_up` function.
- Add `plan_follow_up_window` back to the `_PLANNERS` tuple.
- Revert `on_deal_state_entered` to only handle `PENDING` (remove the `CONNECTED` → `next_follow_up_at` stamp).

The original `plan_follow_up_window`:
```python
def plan_follow_up_window(session, campaign) -> int:
    if _has_pending(Task.TaskType.FOLLOW_UP, campaign.pk):
        return 0
    profile = session.linkedin_profile
    daily_remaining = max(
        0, profile.follow_up_daily_limit - profile._daily_count("follow_up")
    )
    created = _plan_slots(Task.TaskType.FOLLOW_UP, campaign.pk, daily_remaining)
    if created:
        logger.info(
            "[%s] planned %d follow_up slots over next 24h — 1 fires now, "
            "%d Poisson-spaced (daily=%d)",
            campaign, created, max(0, created - 1), daily_remaining,
        )
    return created
```

The original `enqueue_follow_up`:
```python
def enqueue_follow_up(campaign_id: int, public_id: str | None = None, delay_seconds: float = 0) -> None:
    if public_id:
        exists = Task.objects.filter(
            task_type=Task.TaskType.FOLLOW_UP,
            status=Task.Status.PENDING,
            payload__campaign_id=campaign_id,
            payload__public_id=public_id,
        ).exists()
        if exists:
            return
    now = timezone.now()
    Task.objects.create(
        task_type=Task.TaskType.FOLLOW_UP,
        scheduled_at=now + timedelta(seconds=delay_seconds),
        payload={"campaign_id": campaign_id, "public_id": public_id},
    )
```

The original `on_deal_state_entered`:
```python
def on_deal_state_entered(deal) -> None:
    state = ProfileState(deal.state)
    if state != ProfileState.PENDING:
        return
    backoff = deal.backoff_hours or CAMPAIGN_CONFIG["check_pending_recheck_after_hours"]
    deal.next_check_pending_at = timezone.now() + timedelta(hours=backoff)
    deal.save(update_fields=["next_check_pending_at"])
```

The original `_PLANNERS`:
```python
_PLANNERS = (
    plan_connect_window,
    plan_follow_up_window,
    plan_check_pending_window,
    plan_check_messages_window,
)
```

### 3. Restore `linkedin/daemon.py`

```python
from linkedin.tasks.follow_up import handle_follow_up

_HANDLERS = {
    Task.TaskType.CONNECT: handle_connect,
    Task.TaskType.CHECK_PENDING: handle_check_pending,
    Task.TaskType.FOLLOW_UP: handle_follow_up,
    Task.TaskType.CHECK_MESSAGES: handle_check_messages,
}
```

### 4. Restore `linkedin/tasks/check_messages.py`

Revert to the original non-unified version that only handles lead replies:
- Remove `_handle_follow_up_nudge`, `_stamp_next_follow_up`, `_is_follow_up_due` functions.
- Remove the nudge path and orphan scan from `handle_check_messages`.
- Keep only the inbox scan + lead reply path.
- Import `enqueue_follow_up` from `linkedin.tasks.scheduler` instead of using `_stamp_next_follow_up`.
- Import `MIN_FOLLOW_UP_HOURS` from `linkedin.tasks.follow_up`.

The handler should:
1. Scan inbox via Voyager API.
2. Match conversations by `Lead.urn`.
3. For matched conversations with `unreadCount > 0` or `read == false`: sync, check for new incoming messages, reply via follow-up agent.
4. After replying, call `enqueue_follow_up(campaign.pk, public_id, delay_seconds=delay_hours * 3600)`.
5. Skip conversations that are fully read (no nudge logic).

### 5. Restore `linkedin/admin.py`

```python
def resume_follow_up(self, request, queryset):
    from linkedin.tasks.scheduler import enqueue_follow_up
    from django.contrib import messages
    resumed_count = 0
    for deal in queryset:
        if deal.state == "CONNECTED":
            enqueue_follow_up(deal.campaign.pk, deal.lead.public_identifier, delay_seconds=3600)
            resumed_count += 1
    if resumed_count:
        messages.success(request, f"Resumed AI follow-ups for {resumed_count} conversation(s). Next follow-up in 1 hour.")
    else:
        messages.warning(request, "No CONNECTED deals selected.")
```

### 6. Restore `linkedin/management/commands/resume_follow_up.py`

- Import `enqueue_follow_up` from `linkedin.tasks.scheduler`.
- Call `enqueue_follow_up(campaign_id, public_id, delay_seconds=delay)` instead of stamping `next_follow_up_at`.

### 7. Roll back the DB migration

```bash
python manage.py migrate crm 0012
```

### 8. Delete the unused migration file

```bash
rm crm/migrations/0013_deal_next_follow_up_at.py
```

### 9. Remove the `next_follow_up_at` field from `crm/models/deal.py`

Delete the field:
```python
# Remove this line:
next_follow_up_at = models.DateTimeField(null=True, blank=True, db_index=True, ...)
```

### 10. Restore test files

- `tests/tasks/test_tasks.py`: Import `handle_follow_up` from `linkedin.tasks.follow_up`, revert `TestHandleFollowUp` to call `handle_follow_up(task, fake_session, ctx)`.
- `tests/test_reconcile.py`: Revert `test_plans_follow_up_slots` to check `FOLLOW_UP` task count.
- `tests/test_scheduler_advanced.py`: Revert `TestPlanFollowUpWindow` to test `plan_follow_up_window`.

## Files modified by the original merge

- `linkedin/tasks/follow_up.py` — was stripped to constants, restore full original
- `linkedin/tasks/check_messages.py` — was rewritten to be unified, restore original
- `linkedin/tasks/scheduler.py` — had functions removed, add them back
- `linkedin/daemon.py` — had import removed, add back
- `linkedin/admin.py` — had enqueue_follow_up removed, add back
- `linkedin/management/commands/resume_follow_up.py` — same
- `crm/models/deal.py` — had field added, remove field
- `crm/migrations/0013_deal_next_follow_up_at.py` — delete file
- `tests/tasks/test_tasks.py` — updated tests, revert
- `tests/test_reconcile.py` — updated tests, revert
- `tests/test_scheduler_advanced.py` — updated tests, revert
- `CLAUDE.md`, `ARCHITECTURE.md` — doc updates, revert
