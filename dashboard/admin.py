from django.contrib import admin
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


@admin.register(BetaFeedback)
class BetaFeedbackAdmin(admin.ModelAdmin):
    list_display = ('context', 'user', 'created_at', 'message_preview')
    list_filter = ('context', 'created_at')
    search_fields = ('message', 'user__username', 'user__email', 'page_url')
    readonly_fields = ('user', 'context', 'reference_id', 'message', 'page_url', 'created_at')
    date_hierarchy = 'created_at'

    def message_preview(self, obj):
        return obj.message[:80] + ('...' if len(obj.message) > 80 else '')
    message_preview.short_description = 'Message'
