# admin.py
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html
from django.contrib import messages
from .utils_email import send_snapshot_report_email
from .models import AssessmentSession, ResultSnapshot, RecommendationBand, Category, Question, Response, ActionItem, ToolRecommendation, ChatMessage
from .models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation
from dashboard.models import GapAnalysisMetric

@admin.action(description="Resend report email")
def resend_report(modeladmin, request, queryset):
    sent = 0
    for snap in queryset:
        send_snapshot_report_email(snap)
        snap.report_sent = True
        snap.save(update_fields=["report_sent"])
        sent += 1
    messages.success(request, f"Resent {sent} report(s).")

@admin.register(ResultSnapshot)
class ResultSnapshotAdmin(admin.ModelAdmin):
    
    actions = [resend_report]
    
    # List view
    list_display = (
        "company_name",
        "industry",
        "overall",
        "band_stage",
        "created_at",
        "session_short",
    )
    list_select_related = ("session", "band")
    ordering = ("-created_at",)
    date_hierarchy = "created_at"

    # Filters & search (use your new denormalized fields)
    list_filter = (
        "band_stage",
        "industry",
        "country",
        "company_size",
        "revenue_range",
        "crm",
        "created_at",
    )
    search_fields = (
        "company_name",
        "industry",
        "country",
        "contact_name",
        "contact_email",
        "utm_source",
        "utm_campaign",
        "session__uuid",
    )

    # Change form layout (company + contact up top; JSON collapsed)
    fieldsets = (
        (None, {
            "fields": (
                ("session", "overall", "band", "band_stage"),
                "band_headline",
            )
        }),
        ("Company", {
            "fields": (
                "company_name", "industry", "website", "country",
                "company_size", "revenue_range", "crm",
            )
        }),
        ("Contact", {
            "fields": ("contact_name", "contact_email", "contact_role", "phone")
        }),
        ("Attribution", {
            "classes": ("collapse",),
            "fields": ("utm_source", "utm_medium", "utm_campaign", "referrer"),
        }),
        ("Charts / Data", {
            "classes": ("collapse",),
            "fields": ("category_breakdown", "radar_labels", "radar_values"),
        }),
        # ✅ NEW SECTION
        ("AI Output", {
            "classes": ("collapse",),  # optional — remove if you want always visible
            "fields": ("ai_playbook",),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

    readonly_fields = ("created_at", "updated_at", "ai_playbook")
   

    def session_short(self, obj):
        app_label = obj.session._meta.app_label
        model_name = obj.session._meta.model_name  # "assessmentsession"
        url = reverse(f"admin:{app_label}_{model_name}_change", args=[obj.session.pk])
        return format_html('<a href="{}">{}</a>', url, str(obj.session.uuid)[:8])
    session_short.short_description = "Session"

# Optional: show the snapshot on the session page too (readonly inline)
class ResultSnapshotInline(admin.StackedInline):
    model = ResultSnapshot
    can_delete = False
    extra = 0
    readonly_fields = (
        "overall", "band", "band_stage", "band_headline",
        "company_name", "industry", "website", "country",
        "company_size", "revenue_range", "crm",
        "contact_name", "contact_email", "contact_role", "phone",
        "utm_source", "utm_medium", "utm_campaign", "referrer",
        "category_breakdown", "radar_labels", "radar_values",
        "created_at", "updated_at",
    )
    fieldsets = (
        (None, {
            "fields": (
                ("overall", "band", "band_stage"),
                "band_headline",
            )
        }),
        ("Company", {
            "fields": (
                "company_name", "industry", "website", "country",
                "company_size", "revenue_range", "crm",
            )
        }),
        ("Contact", {
            "fields": ("contact_name", "contact_email", "contact_role", "phone")
        }),
        ("Attribution", {
            "classes": ("collapse",),
            "fields": ("utm_source", "utm_medium", "utm_campaign", "referrer"),
        }),
        ("Charts / Data", {
            "classes": ("collapse",),
            "fields": ("category_breakdown", "radar_labels", "radar_values"),
        }),
        ("Timestamps", {
            "classes": ("collapse",),
            "fields": ("created_at", "updated_at"),
        }),
    )

@admin.register(AssessmentSession)
class AssessmentSessionAdmin(admin.ModelAdmin):
    list_display = ("company_name", "industry", "is_completed", "created_at", "snapshot_link")
    search_fields = ("company_name", "industry", "uuid")
    ordering = ("-created_at",)

    inlines = (ResultSnapshotInline,)

    def snapshot_link(self, obj):
        snap = getattr(obj, "snapshot", None)
        if not snap:
            return "—"
        app_label = snap._meta.app_label
        model_name = snap._meta.model_name  # "resultsnapshot"
        url = reverse(f"admin:{app_label}_{model_name}_change", args=[snap.pk])
        return format_html('<a href="{}">Open snapshot</a>', url)
    snapshot_link.short_description = "Snapshot"

# (Optional) register the rest so they're easy to inspect
admin.site.register(RecommendationBand)
admin.site.register(Category)
admin.site.register(Question)
@admin.register(Response)
class ResponseAdmin(admin.ModelAdmin):
    list_display = ("session", "question", "score", "context_note", "ai_insight")
    search_fields = ("session__company_name", "question__text", "context_note", "ai_insight")
    list_filter = ("session", "question")
    readonly_fields = ()
admin.site.register(ActionItem)
admin.site.register(ToolRecommendation)

@admin.register(ChatMessage)
class ChatMessageAdmin(admin.ModelAdmin):
    list_display = ("session", "message_preview", "intent", "created_at")
    list_filter = ("intent", "created_at")
    search_fields = ("message", "response", "session__company_name")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)
    
    def message_preview(self, obj):
        return obj.message[:60] + "..." if len(obj.message) > 60 else obj.message
    message_preview.short_description = "Message"

@admin.register(GapAnalysisMetric)
class GapAnalysisMetricAdmin(admin.ModelAdmin):
    list_display = ('metric', 'category', 'current', 'target', 'priority')
    list_filter = ('category', 'priority')
    search_fields = ('metric', 'recommendation')


# ── Workspace Models ──────────────────────────────────────

class WorkspaceMembershipInline(admin.TabularInline):
    model = WorkspaceMembership
    extra = 0
    readonly_fields = ('invited_at', 'accepted_at')
    raw_id_fields = ('user', 'invited_by')


class WorkspaceInvitationInline(admin.TabularInline):
    model = WorkspaceInvitation
    extra = 0
    readonly_fields = ('token', 'invited_at', 'expires_at', 'accepted_at')
    raw_id_fields = ('invited_by',)


@admin.register(Workspace)
class WorkspaceAdmin(admin.ModelAdmin):
    list_display = ('name', 'slug', 'industry', 'is_active', 'member_count', 'created_at')
    list_filter = ('is_active', 'industry', 'created_at')
    search_fields = ('name', 'slug', 'industry')
    prepopulated_fields = {'slug': ('name',)}
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)
    inlines = [WorkspaceMembershipInline, WorkspaceInvitationInline]

    def member_count(self, obj):
        return WorkspaceMembership.objects.filter(workspace=obj, is_active=True).count()
    member_count.short_description = 'Members'


@admin.register(WorkspaceMembership)
class WorkspaceMembershipAdmin(admin.ModelAdmin):
    list_display = ('user', 'workspace', 'role', 'is_active', 'invited_at')
    list_filter = ('role', 'is_active', 'invited_at')
    search_fields = ('user__username', 'user__email', 'workspace__name')
    raw_id_fields = ('user', 'workspace', 'invited_by')
    readonly_fields = ('invited_at', 'accepted_at')
    ordering = ('-invited_at',)


@admin.register(WorkspaceInvitation)
class WorkspaceInvitationAdmin(admin.ModelAdmin):
    list_display = ('email', 'workspace', 'role', 'invited_by', 'status', 'invited_at', 'expires_at')
    list_filter = ('role', 'is_accepted', 'invited_at')
    search_fields = ('email', 'workspace__name', 'invited_by__username')
    raw_id_fields = ('workspace', 'invited_by')
    readonly_fields = ('token', 'invited_at', 'accepted_at', 'expires_at')
    ordering = ('-invited_at',)

    def status(self, obj):
        if obj.is_accepted or obj.accepted_at:
            return format_html('<span style="color:green;font-weight:bold;">Accepted</span>')
        if obj.is_expired:
            return format_html('<span style="color:red;">Expired</span>')
        return format_html('<span style="color:orange;">Pending</span>')
    status.short_description = 'Status'
