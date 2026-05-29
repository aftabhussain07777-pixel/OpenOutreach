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
from linkedin.models import ActionLog, Campaign, LinkedInProfile
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

        # Build a minimal LinkedInProfile for timestamp-comparison detection.
        from django.contrib.auth.models import User

        user = User.objects.create_user(username="testuser")
        self.profile, _ = LinkedInProfile.objects.get_or_create(
            user=user,
            defaults={
                "linkedin_username": "test@example.com",
                "linkedin_password": "testpass",
            },
        )
        self.session = Mock()
        self.session.linkedin_profile = self.profile
        self.session.campaign = self.campaign

    def _make_action_log(self, minutes_ago=0):
        """Create a FOLLOW_UP ActionLog with an absolute ``created_at``.

        Uses ``update()`` after creation to bypass ``auto_now_add=True``.
        """
        log = ActionLog.objects.create(
            linkedin_profile=self.profile,
            campaign=self.campaign,
            action_type=ActionLog.ActionType.FOLLOW_UP,
        )
        if minutes_ago:
            ActionLog.objects.filter(pk=log.pk).update(
                created_at=timezone.now() - timedelta(minutes=minutes_ago),
            )
        return log

    def test_has_manual_messages_true_when_no_actionlog(self):
        """Outgoing message exists but no AI ActionLog → manual."""
        ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="Manual message",
            is_outgoing=True,
            creation_date=timezone.now() - timedelta(hours=2),
        )
        self.assertTrue(_has_manual_messages_recently(self.deal, self.session))

    def test_has_manual_messages_false_when_timestamps_match(self):
        """Outgoing message and ActionLog have nearby timestamps → AI."""
        now = timezone.now()
        self._make_action_log(minutes_ago=120)
        ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="AI message",
            is_outgoing=True,
            creation_date=now - timedelta(minutes=120),
        )
        self.assertFalse(_has_manual_messages_recently(self.deal, self.session))

    def test_has_manual_messages_true_when_timestamps_mismatch(self):
        """Outgoing message timestamp differs from last ActionLog by >2 min → manual."""
        now = timezone.now()
        # AI action 3 hours ago
        self._make_action_log(minutes_ago=180)
        # Manual message 30 minutes ago (timestamp mismatch > 2 min)
        ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="Manual message",
            is_outgoing=True,
            creation_date=now - timedelta(minutes=30),
        )
        self.assertTrue(_has_manual_messages_recently(self.deal, self.session))

    def test_has_manual_messages_false_no_messages(self):
        """No outgoing messages at all → nothing to detect."""
        self.assertFalse(_has_manual_messages_recently(self.deal, self.session))

    def test_has_manual_messages_ignores_lead_replies(self):
        """Lead replies (is_outgoing=False) are ignored."""
        ChatMessage.objects.create(
            content_type=self.ct,
            object_id=self.lead.pk,
            content="Lead reply",
            is_outgoing=False,
            creation_date=timezone.now() - timedelta(hours=1),
        )
        # No outgoing messages, only incoming → nothing to detect
        self.assertFalse(_has_manual_messages_recently(self.deal, self.session))

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
        template, *args = mock_logger.warning.call_args[0]

        self.assertIn("Manual message detected", template)
        self.assertEqual(args[0], "test-user")  # lead_name
        self.assertIn("This is a manual message", args[1])  # message_preview

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
