from django.contrib import admin, messages
from .models import Channel, ChannelAnalytics, BetaFeedback

@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ('name', 'color')
    search_fields = ('name',)

@admin.register(ChannelAnalytics)
class ChannelAnalyticsAdmin(admin.ModelAdmin):
    list_display = ('channel', 'revenue', 'change', 'date')
    list_filter = ('channel', 'date')
    search_fields = ('channel__name',)


@admin.action(description="Mark selected feedback as reviewed")
def mark_feedback_reviewed(modeladmin, request, queryset):
    n = queryset.update(reviewed=True)
    messages.success(request, f"Marked {n} feedback item(s) reviewed.")


@admin.action(description="Mark selected feedback as unreviewed")
def mark_feedback_unreviewed(modeladmin, request, queryset):
    n = queryset.update(reviewed=False)
    messages.success(request, f"Marked {n} feedback item(s) unreviewed.")


@admin.register(BetaFeedback)
class BetaFeedbackAdmin(admin.ModelAdmin):
    actions = [mark_feedback_reviewed, mark_feedback_unreviewed]
    list_display = ('context', 'user', 'reviewed', 'created_at', 'message_preview')
    list_editable = ('reviewed',)
    list_filter = ('reviewed', 'context', 'created_at')
    search_fields = ('message', 'user__username', 'user__email', 'page_url')
    readonly_fields = ('user', 'context', 'reference_id', 'message', 'page_url', 'created_at')
    date_hierarchy = 'created_at'

    def message_preview(self, obj):
        return obj.message[:80] + ('...' if len(obj.message) > 80 else '')
    message_preview.short_description = 'Message'
