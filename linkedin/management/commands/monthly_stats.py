"""Management command to print monthly outreach stats.

Usage::

    python manage.py monthly_stats [--days 30] [--campaign CAMPAIGN_NAME]

Shows connection requests sent, accepted, messages sent, and replies received.
Defaults to last 30 days; omit --campaign for all campaigns combined.
"""

from collections import OrderedDict
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db.models import Count, Q
from django.utils import timezone


class Command(BaseCommand):
    help = "Print monthly outreach stats (sent, accepted, messaged, replied)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Number of days to look back (default: 30).",
        )
        parser.add_argument(
            "--campaign",
            type=str,
            default=None,
            help="Filter stats to a specific campaign by name.",
        )

    def handle(self, *args, **options):
        from linkedin.models import Campaign, ActionLog
        from crm.models import Deal
        from chat.models import ChatMessage
        from linkedin.enums import ProfileState

        num_days = options["days"]
        since = timezone.now() - timedelta(days=num_days)

        campaign = None
        campaign_filter = Q()
        if options["campaign"]:
            try:
                campaign = Campaign.objects.get(name=options["campaign"])
                campaign_filter = Q(campaign=campaign)
            except Campaign.DoesNotExist:
                self.stderr.write(
                    self.style.ERROR(f"Campaign '{options['campaign']}' not found.")
                )
                return

        # 1. Connection requests sent
        connect_qs = ActionLog.objects.filter(
            action_type=ActionLog.ActionType.CONNECT,
            created_at__gte=since,
        )
        if campaign:
            connect_qs = connect_qs.filter(campaign=campaign)
        connect_sent = connect_qs.count()

        # 2. Connections accepted — Deals that reached CONNECTED or beyond
        #    in the period (approximated by update_date falling in window
        #    while state is CONNECTED+).
        accepted_qs = Deal.objects.filter(
            campaign_filter,
            state__in=(ProfileState.CONNECTED, ProfileState.COMPLETED),
            update_date__gte=since,
        )
        accepted = accepted_qs.count()

        # 3. Messages sent (outgoing ChatMessages)
        lead_ct = ContentType.objects.get_for_model(Deal._meta.get_field("lead").related_model)
        sent_qs = ChatMessage.objects.filter(
            content_type=lead_ct,
            is_outgoing=True,
            creation_date__gte=since,
        )
        if campaign:
            # Narrow to ChatMessages linked to Leads in this campaign's Deals
            campaign_lead_ids = Deal.objects.filter(campaign=campaign).values_list("lead_id", flat=True)
            sent_qs = sent_qs.filter(object_id__in=campaign_lead_ids)
        sent = sent_qs.count()

        # 4. Replies received (incoming ChatMessages)
        reply_qs = ChatMessage.objects.filter(
            content_type=lead_ct,
            is_outgoing=False,
            creation_date__gte=since,
        )
        if campaign:
            reply_qs = reply_qs.filter(object_id__in=campaign_lead_ids)
        replied = reply_qs.count()

        # 5. Campaign breakdown (optional — helps understand per-campaign stats)
        campaign_rows = []
        all_campaigns = Campaign.objects.all()
        for c in all_campaigns:
            c_connect = ActionLog.objects.filter(
                action_type=ActionLog.ActionType.CONNECT,
                campaign=c,
                created_at__gte=since,
            ).count()
            c_accepted = Deal.objects.filter(
                campaign=c,
                state__in=(ProfileState.CONNECTED, ProfileState.COMPLETED),
                update_date__gte=since,
            ).count()
            c_lead_ids = Deal.objects.filter(campaign=c).values_list("lead_id", flat=True)
            c_sent = ChatMessage.objects.filter(
                content_type=lead_ct,
                object_id__in=c_lead_ids,
                is_outgoing=True,
                creation_date__gte=since,
            ).count()
            c_replied = ChatMessage.objects.filter(
                content_type=lead_ct,
                object_id__in=c_lead_ids,
                is_outgoing=False,
                creation_date__gte=since,
            ).count()
            if c_connect > 0 or c_sent > 0:
                campaign_rows.append((c.name, c_connect, c_accepted, c_sent, c_replied))

        # ── Print ──────────────────────────────────────────────────────
        title = f"📊 Outreach Stats — Last {num_days} Days"
        if campaign:
            title += f"  (campaign: {campaign.name})"
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(title))
        self.stdout.write("")

        # Summary table
        col_w = {"Metric": 30, "Count": 10}
        sep = "  " + "─" * (col_w["Metric"] + col_w["Count"] + 5)

        rows = [
            ("🔗 Connection requests sent", connect_sent),
            ("✅ Connections accepted", accepted),
            ("📨 Messages sent (outgoing)", sent),
            ("💬 Replies received (incoming)", replied),
        ]

        if connect_sent > 0:
            accept_rate = round(accepted / connect_sent * 100)
        else:
            accept_rate = 0
        if sent > 0:
            reply_rate = round(replied / sent * 100)
        else:
            reply_rate = 0

        rows.append(("📈 Acceptance rate", f"{accept_rate}%"))
        rows.append(("📈 Reply rate (of messages sent)", f"{reply_rate}%"))

        for metric, value in rows:
            self.stdout.write(
                f"  {metric:<{col_w['Metric']}}  {str(value):>{col_w['Count']}}"
            )

        # Per-campaign breakdown
        if not campaign and campaign_rows:
            self.stdout.write("")
            self.stdout.write(self.style.MIGRATE_HEADING("  Per-Campaign Breakdown"))
            self.stdout.write("")
            hdr = (
                f"  {'Campaign':<30}  "
                f"{'Sent':>6}  {'Accepted':>9}  "
                f"{'Msgs':>5}  {'Replies':>8}"
            )
            self.stdout.write(hdr)
            self.stdout.write("  " + "─" * (30 + 6 + 9 + 5 + 8 + 9))
            for name, snt, acc, msg, rep in campaign_rows:
                self.stdout.write(
                    f"  {name:<30}  "
                    f"{snt:>6}  {acc:>9}  "
                    f"{msg:>5}  {rep:>8}"
                )

        self.stdout.write("")
