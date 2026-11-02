# linkedin/management/commands/postclone_setup.py
"""Import-day DB neutralization for a cloned local DB: flush stale pending
tasks, ramp daily limits, stagger deal timers into the future."""

import random
from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from crm.models import Deal
from linkedin.models import LinkedInProfile, Task


class Command(BaseCommand):
    help = (
        "Post-clone cleanup: flush stale pending tasks to FAILED, set ramped "
        "daily limits on the active profile, stagger next_follow_up_at / "
        "next_check_pending_at forward so the daemon starts a slow trickle "
        "instead of a boot-day burst. Aborts before any write if the clone "
        "is not in place."
    )

    def add_arguments(self, parser):
        parser.add_argument("--connect-limit", type=int, default=5)
        parser.add_argument("--follow-up-limit", type=int, default=15)
        parser.add_argument("--follow-up-stagger-days", type=int, default=14)
        parser.add_argument("--check-pending-stagger-days", type=int, default=4)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        profiles = LinkedInProfile.objects.filter(active=True)
        if profiles.count() != 1:
            raise CommandError(
                f"Expected exactly 1 active LinkedInProfile, found "
                f"{profiles.count()} — the cloned db.sqlite3 is not in place. "
                "Nothing was written."
            )
        profile = profiles.first()

        pending = Task.objects.filter(status=Task.Status.PENDING)
        pending_counts = Counter(pending.values_list("task_type", flat=True))
        pending_total = pending.count()

        limits_before = (profile.connect_daily_limit, profile.follow_up_daily_limit)

        deals = list(Deal.objects.only("id", "state"))
        state_counts = Counter(str(deal.state) for deal in deals)

        now = timezone.now()
        fu_span = options["follow_up_stagger_days"]
        cp_span = options["check_pending_stagger_days"]

        self.stdout.write(f"Profile   : {profile} (id={profile.id}, active)")
        self.stdout.write(f"Pending   : {pending_total} stale tasks "
                          f"{dict(pending_counts)} -> will be marked FAILED")
        self.stdout.write(f"Limits    : connect {limits_before[0]} -> "
                          f"{options['connect_limit']}, follow_up "
                          f"{limits_before[1]} -> {options['follow_up_limit']}")
        self.stdout.write(f"Deals     : {len(deals)} total {dict(state_counts)} -> "
                          f"next_follow_up_at now+2-{fu_span}d, "
                          f"next_check_pending_at now+1-{cp_span}d (trickle)")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing written."))
            return

        modified = pending.update(status=Task.Status.FAILED)
        profile.connect_daily_limit = options["connect_limit"]
        profile.follow_up_daily_limit = options["follow_up_limit"]
        profile.save(update_fields=["connect_daily_limit", "follow_up_daily_limit"])

        deals = list(
            Deal.objects.only("id", "next_follow_up_at", "next_check_pending_at")
        )
        for deal in deals:
            deal.next_follow_up_at = now + timedelta(
                days=random.uniform(2, fu_span)
            )
            deal.next_check_pending_at = now + timedelta(
                days=random.uniform(1, cp_span)
            )
        Deal.objects.bulk_update(
            deals, ["next_follow_up_at", "next_check_pending_at"]
        )

        self.stdout.write(self.style.SUCCESS(
            f"Done. {modified} pending task(s) flushed, limits set "
            f"({options['connect_limit']}/{options['follow_up_limit']}), "
            f"{len(deals)} deal timer(s) staggered."
        ))
