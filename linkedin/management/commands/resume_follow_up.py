# linkedin/management/commands/resume_follow_up.py
"""Resume AI follow-ups for a specific conversation after manual intervention."""
from __future__ import annotations

import logging
from django.core.management.base import BaseCommand, CommandError
from django.contrib.contenttypes.models import ContentType

from crm.models import Deal
from linkedin.models import Campaign
from linkedin.tasks.scheduler import enqueue_follow_up

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Resume AI follow-ups for a specific conversation after manual intervention"

    def add_arguments(self, parser):
        parser.add_argument(
            "campaign_id",
            type=int,
            help="Campaign ID",
        )
        parser.add_argument(
            "public_identifier",
            type=str,
            help="Lead public identifier (LinkedIn profile URL part)",
        )
        parser.add_argument(
            "--delay",
            type=int,
            default=3600,
            help="Delay before first follow-up (seconds, default: 3600)",
        )

    def handle(self, *args, **options):
        campaign_id = options["campaign_id"]
        public_id = options["public_identifier"]
        delay = options["delay"]

        try:
            campaign = Campaign.objects.get(pk=campaign_id)
        except Campaign.DoesNotExist:
            raise CommandError(f"Campaign {campaign_id} does not exist")

        deal = (
            Deal.objects.filter(
                lead__public_identifier=public_id,
                campaign=campaign,
            )
            .select_related("lead", "campaign")
            .first()
        )

        if not deal:
            raise CommandError(f"No deal found for {public_id} in campaign {campaign.name}")

        # Check if there are manual messages that would cause immediate pause
        from chat.models import ChatMessage
        from django.utils import timezone
        from datetime import timedelta

        ct = ContentType.objects.get_for_model(deal.lead)
        recent_manual = ChatMessage.objects.filter(
            content_type=ct,
            object_id=deal.lead_id,
            source="manual",
            is_outgoing=True,
            creation_date__gte=timezone.now() - timedelta(hours=24)
        ).exists()

        if recent_manual:
            self.stdout.write(
                self.style.WARNING(
                    f"⚠️  Warning: Manual messages detected in last 24 hours for {public_id}\n"
                    f"AI will pause again on next follow-up attempt.\n"
                    f"Consider waiting 24 hours or clearing manual messages."
                )
            )

        # Resume follow-ups
        enqueue_follow_up(campaign_id, public_id, delay_seconds=delay)

        self.stdout.write(
            self.style.SUCCESS(
                f"✅ AI follow-ups resumed for {public_id} in campaign {campaign.name}\n"
                f"Next follow-up in {delay//3600} hours"
            )
        )

        # Show conversation status
        total_messages = ChatMessage.objects.filter(
            content_type=ct, object_id=deal.lead_id
        ).count()
        ai_messages = ChatMessage.objects.filter(
            content_type=ct, object_id=deal.lead_id, source="ai"
        ).count()
        manual_messages = ChatMessage.objects.filter(
            content_type=ct, object_id=deal.lead_id, source="manual"
        ).count()

        self.stdout.write(f"📊 Conversation stats:")
        self.stdout.write(f"   Total messages: {total_messages}")
        self.stdout.write(f"   AI messages: {ai_messages}")
        self.stdout.write(f"   Manual messages: {manual_messages}")
