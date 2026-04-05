from django.contrib import admin
from .models import Channel, ChannelAnalytics

@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ('name', 'color')
    search_fields = ('name',)

@admin.register(ChannelAnalytics)
class ChannelAnalyticsAdmin(admin.ModelAdmin):
    list_display = ('channel', 'revenue', 'change', 'date')
    list_filter = ('channel', 'date')
    search_fields = ('channel__name',)
