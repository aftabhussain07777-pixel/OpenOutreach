# tests/test_manual_intervention.py
"""Tests for manual intervention detection and follow-up pause functionality."""

from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.utils import timezone

from chat.models import ChatMessage
from crm.models import Deal, Lead
from linkedin.actions.message import _mark_message_as_ai
from linkedin.models import Campaign, LinkedInProfile
from linkedin.tasks.follow_up import (
    _has_manual_messages_recently,
    _notify_manual_intervention,
)


class ManualInterventionTestCase(TestCase):
    """Test manual intervention detection and pause functionality."""

    def setUp(self):
        """Set up test data."""
        self.campaign = Campaign.objects.create(
            name="Test Campaign",
            product_docs="Test product",
            campaign_objective="Test objective",
        )
        self.lead = Lead.objects.create(
            public_identifier="test-user",
            linkedin_url="https://linkedin.com/in/test-user",
        )
        self.deal = Deal.objects.create(
            lead=self.lead, campaign=self.campaign, state="CONNECTED"
        )
        self.ct = ContentType.objects.get_for_model(Lead)

    def test_has_manual_messages_recently_true(self):
        """Test detection when manual messages exist."""
        # Create a manual message from 2 hours ago
        ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="Manual message",
            source="manual",
            is_outgoing=True,
            creation_date=timezone.now() - timedelta(hours=2),
        )

        self.assertTrue(_has_manual_messages_recently(self.deal))

    def test_has_manual_messages_recently_false_with_ai_only(self):
        """Test no detection when only AI messages exist."""
        # Create an AI message from 2 hours ago
        ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="AI message",
            source="ai",
            is_outgoing=True,
            creation_date=timezone.now() - timedelta(hours=2),
        )

        self.assertFalse(_has_manual_messages_recently(self.deal))

    def test_has_manual_messages_recently_false_old_messages(self):
        """Test no detection when manual messages are older than 24 hours."""
        # Create a manual message from 2 days ago
        ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="Old manual message",
            source="manual",
            is_outgoing=True,
            creation_date=timezone.now() - timedelta(days=2),
        )

        self.assertFalse(_has_manual_messages_recently(self.deal))

    def test_has_manual_messages_recently_false_no_messages(self):
        """Test no detection when no messages exist."""
        self.assertFalse(_has_manual_messages_recently(self.deal))

    @patch("linkedin.tasks.follow_up.logger")
    def test_notify_manual_intervention(self, mock_logger):
        """Test manual intervention notification."""
        # Create a manual message
        manual_msg = ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="This is a manual message that should trigger notification",
            source="manual",
            is_outgoing=True,
            creation_date=timezone.now() - timedelta(hours=1),
        )

        # Mock session
        session = Mock()
        session.campaign = self.campaign

        # Call notification function
        _notify_manual_intervention(session, self.deal, "test-user")

        # Verify logger was called with expected message
        mock_logger.warning.assert_called_once()
        # The format string uses %s placeholders — check both the template
        # and the positional args.
        template, *args = mock_logger.warning.call_args[0]

        self.assertIn("AI PAUSED", template)
        self.assertEqual(args[0], "test-user")  # lead_name
        self.assertIn("This is a manual message", args[1])  # message_preview
        self.assertEqual(args[2], self.campaign.name)  # campaign name

    @patch("linkedin.actions.message.logger")
    def test_mark_message_as_ai(self, mock_logger):
        """Test marking AI messages in database."""
        # Create a message that looks like it could be AI
        message = ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="AI-generated follow-up message",
            source="manual",  # Initially marked as manual
            is_outgoing=True,
            creation_date=timezone.now() - timedelta(minutes=2),
        )

        # Mock session
        session = Mock()

        # Call marking function
        _mark_message_as_ai(session, "test-user", "AI-generated follow-up message")

        # Verify message was updated
        message.refresh_from_db()
        self.assertEqual(message.source, "ai")

    def test_source_field_choices(self):
        """Test ChatMessage source field has correct choices."""
        field = ChatMessage._meta.get_field("source")
        choices = field.choices
        expected_choices = [("ai", "AI"), ("manual", "Manual")]
        self.assertEqual(choices, expected_choices)

    def test_source_field_default(self):
        """Test ChatMessage source field defaults to manual."""
        message = ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="Test message",
            is_outgoing=True,
        )
        self.assertEqual(message.source, "manual")


if __name__ == "__main__":
    pytest.main([__file__])
