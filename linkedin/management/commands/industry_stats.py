"""Management command to print industry-level stats for a campaign.

Usage::

    python manage.py industry_stats [--campaign CAMPAIGN_NAME]

If no campaign is specified, stats are shown for all campaigns combined.
"""

from collections import OrderedDict

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db.models import Count, Exists, OuterRef, Q


def _industry_stats(campaign=None):
    """Return a list of OrderedDict rows with industry stats.

    Each row has: industry, total, accepted, replied, meetings.
    Percentages are formatted as strings e.g. "38%".
    """
    from crm.models import Deal, Outcome, Lead
    from chat.models import ChatMessage
    from linkedin.enums import ProfileState

    lead_ct = ContentType.objects.get_for_model(Lead)

    # Subquery: does this Deal's Lead have at least one incoming ChatMessage?
    replied_subq = Exists(
        ChatMessage.objects.filter(
            content_type=lead_ct,
            object_id=OuterRef("lead_id"),
            is_outgoing=False,
        )
    )

    # Base queryset — all Deals that have a Lead with a known industry
    qs = Deal.objects.filter(lead__industry__gt="").select_related("lead")

    if campaign is not None:
        qs = qs.filter(campaign=campaign)

    # Aggregate per industry
    rows = (
        qs.values("lead__industry")
        .annotate(
            total=Count("id"),
            accepted=Count("id", filter=~Q(state=ProfileState.FAILED)),
            replied=Count("id", filter=replied_subq),
            meetings=Count("id", filter=Q(outcome=Outcome.CONVERTED)),
        )
        .order_by("-total")
    )

    results = []
    for r in rows:
        total = r["total"]
        if total == 0:
            continue
        accepted_pct = round(r["accepted"] / total * 100) if r["accepted"] else 0
        replied_pct = round(r["replied"] / total * 100) if r["replied"] else 0
        results.append(OrderedDict([
            ("Industry", r["lead__industry"]),
            ("Accepted", f"{accepted_pct}%"),
            ("Replied", f"{replied_pct}%"),
            ("Meetings", r["meetings"]),
        ]))

    return results


class Command(BaseCommand):
    help = "Print industry-level outreach stats as a table."

    def add_arguments(self, parser):
        parser.add_argument(
            "--campaign",
            type=str,
            default=None,
            help="Filter stats to a specific campaign by name.",
        )

    def handle(self, *args, **options):
        from linkedin.models import Campaign

        campaign = None
        if options["campaign"]:
            try:
                campaign = Campaign.objects.get(name=options["campaign"])
            except Campaign.DoesNotExist:
                self.stderr.write(
                    self.style.ERROR(f"Campaign '{options['campaign']}' not found.")
                )
                return

        rows = _industry_stats(campaign)
        if not rows:
            self.stdout.write("No industry data yet. Leads will be tagged with their\n"
                              "industry as they are discovered and enriched.")
            return

        # Column widths (minimums)
        col_w = {
            "Industry": max(10, max(len(r["Industry"]) for r in rows)),
            "Accepted": 10,
            "Replied": 9,
            "Meetings": 10,
        }
        full_w = sum(col_w.values()) + 7  # 3 borders + 4 padding

        header = (
            f"  {'Industry':<{col_w['Industry']}}  "
            f"{'Accepted':>{col_w['Accepted']}}  "
            f"{'Replied':>{col_w['Replied']}}  "
            f"{'Meetings':>{col_w['Meetings']}}"
        )

        self.stdout.write("")
        self.stdout.write(header)
        self.stdout.write("  " + "─" * (full_w - 2))

        for r in rows:
            line = (
                f"  {r['Industry']:<{col_w['Industry']}}  "
                f"{r['Accepted']:>{col_w['Accepted']}}  "
                f"{r['Replied']:>{col_w['Replied']}}  "
                f"{r['Meetings']:>{col_w['Meetings']}}"
            )
            self.stdout.write(line)

        self.stdout.write("")
