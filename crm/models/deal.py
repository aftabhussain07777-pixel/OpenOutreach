from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from linkedin.enums import ProfileState


class Outcome(models.TextChoices):
    CONVERTED = "converted"
    NOT_INTERESTED = "not_interested"
    WRONG_FIT = "wrong_fit"
    NO_BUDGET = "no_budget"
    HAS_SOLUTION = "has_solution"
    BAD_TIMING = "bad_timing"
    UNRESPONSIVE = "unresponsive"
    UNKNOWN = "unknown"


class Deal(models.Model):
    class Meta:
        verbose_name = _("Deal")
        verbose_name_plural = _("Deals")
        constraints = [
            models.UniqueConstraint(fields=["lead", "campaign"], name="unique_deal_per_campaign"),
        ]

    lead = models.ForeignKey("Lead", on_delete=models.CASCADE)
    campaign = models.ForeignKey(
        "linkedin.Campaign", on_delete=models.CASCADE, related_name="deals",
    )
    state = models.CharField(
        max_length=20,
        choices=[(s.value, s.value) for s in ProfileState],
        default=ProfileState.QUALIFIED,
    )
    outcome = models.CharField(
        max_length=20,
        choices=Outcome.choices,
        blank=True,
        default="",
    )
    reason = models.TextField(blank=True, default="")
    connect_attempts = models.IntegerField(default=0)
    backoff_hours = models.IntegerField(default=0)
    unanswered_follow_up_count = models.IntegerField(
        default=0,
        help_text="Number of consecutive AI follow-ups without a reply"
    )
    next_check_pending_at = models.DateTimeField(null=True, blank=True, db_index=True)
    next_follow_up_at = models.DateTimeField(
        null=True, blank=True, db_index=True,
        help_text="When the next nudge/first-message is due (unified check_messages handler)",
    )
    profile_summary = models.JSONField(null=True, blank=True, default=None)
    chat_summary = models.JSONField(null=True, blank=True, default=None)
    agent_user_states = models.JSONField(
        null=True, blank=True, default=None,
        help_text="Last agent user states (RecipientState, ConversationState, RelationshipState, BusinessState)",
    )
    agent_objective_category = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Last agent objective category",
    )
    agent_objective = models.TextField(
        blank=True, default="",
        help_text="Last agent objective description",
    )
    agent_reasoning_summary = models.TextField(
        blank=True, default="",
        help_text="Last agent reasoning summary",
    )
    creation_date = models.DateTimeField(default=timezone.now)
    update_date = models.DateTimeField(auto_now=True)

    def __str__(self):
        lead_str = str(self.lead) if self.lead_id else "?"
        return f"{lead_str} [{self.state}]"
