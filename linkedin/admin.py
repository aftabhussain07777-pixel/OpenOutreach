# linkedin/admin.py
from django.contrib import admin

from chat.models import ChatMessage

from linkedin.models import ActionLog, Campaign, LinkedInProfile, SearchKeyword, SiteConfig, Task
from crm.models import Deal


@admin.register(SiteConfig)
class SiteConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "llm_provider", "ai_model", "llm_api_base")

    def has_add_permission(self, request):
        return not SiteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ("name", "booking_link")
    filter_horizontal = ("users",)


@admin.register(LinkedInProfile)
class LinkedInProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "linkedin_username", "active", "legal_accepted")
    list_filter = ("active",)
    raw_id_fields = ("user", "self_lead")


@admin.register(SearchKeyword)
class SearchKeywordAdmin(admin.ModelAdmin):
    list_display = ("keyword", "campaign", "used", "used_at")
    list_filter = ("used", "campaign")
    raw_id_fields = ("campaign",)


@admin.register(ActionLog)
class ActionLogAdmin(admin.ModelAdmin):
    list_display = ("action_type", "linkedin_profile", "campaign", "created_at")
    list_filter = ("action_type", "campaign")
    raw_id_fields = ("linkedin_profile", "campaign")
    date_hierarchy = "created_at"
    readonly_fields = ("linkedin_profile", "campaign", "action_type", "created_at")


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    list_display = ("task_type", "status", "scheduled_at", "payload", "created_at")
    list_filter = ("task_type", "status")
    readonly_fields = (
        "task_type", "status", "scheduled_at", "payload",
        "created_at", "started_at", "completed_at",
    )
    date_hierarchy = "scheduled_at"


@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("content_type", "object_id", "owner", "source", "is_outgoing", "creation_date")
    list_filter = ("content_type", "owner", "source", "is_outgoing")
    raw_id_fields = ("owner", "answer_to", "topic")
    date_hierarchy = "creation_date"
    readonly_fields = ("content_type", "object_id", "content", "owner", "creation_date")


@admin.register(Deal)
class DealAdmin(admin.ModelAdmin):
    list_display = ("lead", "campaign", "state", "outcome", "connect_attempts", "update_date")
    list_filter = ("state", "outcome", "campaign")
    raw_id_fields = ("lead", "campaign")
    date_hierarchy = "update_date"
    readonly_fields = ("lead", "campaign", "state", "outcome", "connect_attempts", "creation_date", "update_date")
    
    actions = ["resume_follow_up"]
    
    def resume_follow_up(self, request, queryset):
        """Resume AI follow-ups for selected deals."""
        from linkedin.tasks.scheduler import enqueue_follow_up
        from django.contrib import messages
        
        resumed_count = 0
        for deal in queryset:
            if deal.state == "CONNECTED":
                enqueue_follow_up(deal.campaign.pk, deal.lead.public_identifier, delay_seconds=3600)
                resumed_count += 1
        
        if resumed_count:
            messages.success(
                request, 
                f"✅ Resumed AI follow-ups for {resumed_count} conversation(s). "
                f"Next follow-up in 1 hour."
            )
        else:
            messages.warning(
                request, 
                "⚠️ No CONNECTED deals selected. Only connected conversations can be resumed."
            )
    
    resume_follow_up.short_description = "Resume AI follow-ups"
