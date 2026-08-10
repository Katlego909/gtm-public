# admin.py
from django.contrib import admin
from django.urls import reverse, path
from django.shortcuts import redirect
from django.utils import timezone
from django.utils.html import format_html, format_html_join
from django.contrib import messages
from django.db.models import Q, F, Sum, Count
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from .utils_email import send_snapshot_report_email
from .models import AssessmentSession, ResultSnapshot, RecommendationBand, Category, Question, Response, ActionItem, ToolRecommendation, ChatMessage, WorkspaceChatMessage, AgentDocument
from .models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation
from .models_ai_credits import AICreditAccount, AICreditTransaction
from .models_ai_locks import AIGenerationLock
from .models_beta import BetaInviteCode
from dashboard.models import GapAnalysisMetric, BetaFeedback

User = get_user_model()

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
        # New section
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

@admin.action(description="Reset stuck AI generation (clear locks)")
def reset_stuck_ai(modeladmin, request, queryset):
    """Clear leftover AI generation locks for the selected sessions.

    Locks are keyed by snapshot id (playbook/enrichment) and response id
    (diagnostic) -- see gtm/ai_services.py -- so map each session to those
    ids and delete any matching lock rows, freeing the content to regenerate.
    """
    total = 0
    for session in queryset:
        ids = []
        snap = getattr(session, "snapshot", None)
        if snap:
            ids.append(str(snap.id))
        ids += [str(pk) for pk in session.responses.values_list("id", flat=True)]
        if not ids:
            continue
        q = Q()
        for i in ids:
            q |= Q(lock_key__icontains=i)
        deleted, _ = AIGenerationLock.objects.filter(q).delete()
        total += deleted
    messages.success(
        request,
        f"Cleared {total} stuck AI generation lock(s) across {queryset.count()} session(s).",
    )


@admin.register(AssessmentSession)
class AssessmentSessionAdmin(admin.ModelAdmin):
    actions = [reset_stuck_ai]
    list_display = ("company_name", "industry", "user", "is_completed", "created_at", "snapshot_link")
    list_filter = ("is_completed", "created_at")
    search_fields = ("company_name", "industry", "uuid", "user__username", "user__email")
    ordering = ("-created_at",)
    raw_id_fields = ("user", "workspace")

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


@admin.register(WorkspaceChatMessage)
class WorkspaceChatMessageAdmin(admin.ModelAdmin):
    list_display = ("workspace", "agent_type", "message_preview", "intent", "created_at")
    list_filter = ("agent_type", "intent", "created_at")
    search_fields = ("message", "response", "workspace__name")
    readonly_fields = ("created_at",)
    ordering = ("-created_at",)

    def message_preview(self, obj):
        return obj.message[:60] + "..." if len(obj.message) > 60 else obj.message
    message_preview.short_description = "Message"


@admin.register(AgentDocument)
class AgentDocumentAdmin(admin.ModelAdmin):
    list_display = ("title", "agent_type", "doc_type", "version", "workspace", "updated_at")
    list_filter = ("agent_type", "doc_type", "created_at")
    search_fields = ("title", "content", "workspace__name")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("-updated_at",)


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


@admin.action(description="Reset current period (clear usage now)")
def reset_credit_period(modeladmin, request, queryset):
    from django.utils import timezone
    count = queryset.update(tokens_used=0, period_started_at=timezone.now())
    messages.success(request, f"Reset the current period for {count} account(s).")


class HighUsageFilter(admin.SimpleListFilter):
    """Spot accounts that are running low on their token budget this period."""
    title = "usage level"
    parameter_name = "usage_level"

    def lookups(self, request, model_admin):
        return (("high", ">= 80% used"), ("exhausted", "Exhausted (100%)"))

    def queryset(self, request, qs):
        value = self.value()
        if value == "high":
            return qs.filter(tokens_used__gte=F("token_budget") * 0.8)
        if value == "exhausted":
            return qs.filter(tokens_used__gte=F("token_budget"))
        return qs


@admin.register(AICreditAccount)
class AICreditAccountAdmin(admin.ModelAdmin):
    actions = [reset_credit_period]
    list_display = ('__str__', 'tier', 'period_length', 'usage_display', 'remaining_display', 'period_started_at', 'updated_at')
    list_filter = (HighUsageFilter, 'tier', 'period_length')
    list_select_related = ('workspace', 'user')
    search_fields = ('workspace__name', 'user__username', 'user__email')
    readonly_fields = ('id', 'tokens_used', 'period_started_at', 'created_at', 'updated_at')

    def usage_display(self, obj):
        budget = obj.token_budget or 0
        used = obj.tokens_used or 0
        pct = round(used / budget * 100) if budget else 0
        color = "#b91c1c" if pct >= 80 else ("#b45309" if pct >= 50 else "#15803d")
        # Raw tokens are primary here (this is the operator surface that tunes
        # token_budget in tokens); the credit equivalent is shown alongside.
        return format_html(
            '<span style="color:{};">{} / {} tokens ({}%) &middot; {} / {} credits</span>',
            color, f"{used:,}", f"{budget:,}", pct,
            f"{AICreditAccount.tokens_to_credits(used):,}",
            f"{AICreditAccount.tokens_to_credits(budget):,}",
        )
    usage_display.short_description = "Usage (period)"

    def remaining_display(self, obj):
        remaining = max(0, (obj.token_budget or 0) - (obj.tokens_used or 0))
        return f"{remaining:,} tokens ({AICreditAccount.tokens_to_credits(remaining):,} credits)"
    remaining_display.short_description = "Remaining"
    # tier, token_budget and period_length stay editable -- this is the manual
    # adjustment surface for v1 (no self-serve UI): change tier to re-grant a
    # preset budget, raise token_budget for a one-off override, or use the
    # "Reset current period" action to zero out tokens_used immediately.


@admin.register(AICreditTransaction)
class AICreditTransactionAdmin(admin.ModelAdmin):
    list_display = ('account', 'feature', 'tokens_spent', 'session', 'actor', 'created_at')
    list_filter = ('feature', 'created_at')
    list_select_related = ('account', 'session', 'actor')
    search_fields = ('account__workspace__name', 'account__user__username')
    date_hierarchy = 'created_at'


@admin.action(description="Email invite code to the address on file")
def email_invite_code(modeladmin, request, queryset):
    from .utils_email import send_beta_invite_email
    sent, skipped = 0, 0
    for invite in queryset:
        if not invite.email:
            skipped += 1
            continue
        send_beta_invite_email(invite, invite.email, request=request)
        sent += 1
    if sent:
        messages.success(request, f"Sent {sent} invite email(s).")
    if skipped:
        messages.warning(request, f"Skipped {skipped} code(s) with no email on file.")


class RedemptionStatusFilter(admin.SimpleListFilter):
    """Filter invite codes by unused / used / expired-and-unused."""
    title = "redemption status"
    parameter_name = "redemption"

    def lookups(self, request, model_admin):
        return (("unused", "Unused"), ("used", "Used"), ("expired", "Expired (unused)"))

    def queryset(self, request, qs):
        now = timezone.now()
        value = self.value()
        if value == "used":
            return qs.filter(used_by__isnull=False)
        if value == "unused":
            return qs.filter(used_by__isnull=True).filter(
                Q(expires_at__isnull=True) | Q(expires_at__gt=now)
            )
        if value == "expired":
            return qs.filter(used_by__isnull=True, expires_at__isnull=False, expires_at__lte=now)
        return qs


@admin.register(BetaInviteCode)
class BetaInviteCodeAdmin(admin.ModelAdmin):
    actions = [email_invite_code]
    change_list_template = "admin/gtm/betainvitecode/change_list.html"
    list_display = ('code', 'email', 'note', 'status_badge', 'used_by', 'created_at', 'expires_at')
    list_filter = (RedemptionStatusFilter, 'created_at')
    search_fields = ('code', 'email', 'note', 'used_by__username', 'used_by__email')
    readonly_fields = ('used_by', 'used_at', 'created_at')

    def status_badge(self, obj):
        if obj.is_used:
            return format_html('<span style="color:#6b7280;">Used</span>')
        if obj.is_expired:
            return format_html('<span style="color:#b91c1c;">Expired</span>')
        return format_html('<span style="color:#15803d;font-weight:600;">Unused</span>')
    status_badge.short_description = "Status"

    def get_urls(self):
        urls = super().get_urls()
        extra = [
            path(
                "generate/",
                self.admin_site.admin_view(self.generate_codes_view),
                name="gtm_betainvitecode_generate",
            ),
        ]
        return extra + urls

    def generate_codes_view(self, request):
        """One-click bulk generation from the changelist toolbar (?count=N)."""
        try:
            count = max(1, min(100, int(request.GET.get("count", 5))))
        except (TypeError, ValueError):
            count = 5
        created = [BetaInviteCode.objects.create(note="Generated from admin") for _ in range(count)]
        self.message_user(
            request,
            f"Generated {len(created)} invite code(s): " + ", ".join(c.code for c in created),
            level=messages.SUCCESS,
        )
        return redirect(reverse("admin:gtm_betainvitecode_changelist"))


# ── AI generation locks (support: clear stuck generation) ─────────

@admin.register(AIGenerationLock)
class AIGenerationLockAdmin(admin.ModelAdmin):
    list_display = ("lock_key", "expires_at", "is_expired", "created_at")
    search_fields = ("lock_key",)
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")

    def is_expired(self, obj):
        return obj.expires_at <= timezone.now()
    is_expired.boolean = True
    is_expired.short_description = "Expired"


# ── User admin: tester activity at a glance (support/moderation) ──

try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class CustomUserAdmin(DjangoUserAdmin):
    list_display = (
        "username", "email", "is_active", "is_staff",
        "date_joined", "assessment_count", "workspaces",
    )
    list_filter = DjangoUserAdmin.list_filter + ("date_joined",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            _assessment_count=Count("gtm_sessions", distinct=True)
        )

    def assessment_count(self, obj):
        return obj._assessment_count
    assessment_count.short_description = "Assessments"
    assessment_count.admin_order_field = "_assessment_count"

    def workspaces(self, obj):
        memberships = (
            WorkspaceMembership.objects
            .filter(user=obj, is_active=True)
            .select_related("workspace")
        )
        if not memberships:
            return "—"
        return format_html_join(
            ", ", '<a href="{}">{}</a>',
            (
                (reverse("admin:gtm_workspace_change", args=[m.workspace_id]), m.workspace.name)
                for m in memberships
            ),
        )
    workspaces.short_description = "Workspaces"


# ── Beta Ops Dashboard (aggregate view at /admin/beta-ops/) ───────

def beta_ops_dashboard(request):
    """Read-only at-a-glance view of the beta: signups, invite codes,
    workspaces, assessments, and Vertex AI token spend."""
    from datetime import timedelta

    now = timezone.now()
    today = now.date()
    week_ago = now - timedelta(days=7)

    assessments_total = AssessmentSession.objects.count()
    assessments_done = AssessmentSession.objects.filter(is_completed=True).count()

    tiles = [
        ("Total users", f"{User.objects.count():,}", ""),
        ("Signups today", f"{User.objects.filter(date_joined__date=today).count():,}", ""),
        ("Signups (7d)", f"{User.objects.filter(date_joined__gte=week_ago).count():,}", ""),
        ("Invite codes used", f"{BetaInviteCode.objects.filter(used_by__isnull=False).count():,}", ""),
        ("Invite codes unused", f"{BetaInviteCode.objects.filter(used_by__isnull=True).count():,}", ""),
        ("Active workspaces", f"{Workspace.objects.filter(is_active=True).count():,}", ""),
        ("Assessments completed", f"{assessments_done:,}", f"of {assessments_total:,} total"),
        ("Vertex AI tokens spent", f"{AICreditTransaction.objects.aggregate(t=Sum('tokens_spent'))['t'] or 0:,}", "all time"),
        ("Feedback (7d)", f"{BetaFeedback.objects.filter(created_at__gte=week_ago).count():,}", ""),
        ("Feedback unreviewed", f"{BetaFeedback.objects.filter(reviewed=False).count():,}", ""),
    ]

    top_spenders = list(
        AICreditAccount.objects
        .select_related("workspace", "user")
        .order_by("-tokens_used")[:5]
    )

    context = {
        **admin.site.each_context(request),
        "title": "Beta Ops Dashboard",
        "tiles": tiles,
        "top_spenders": top_spenders,
    }
    from django.shortcuts import render
    return render(request, "admin/beta_ops.html", context)


# Register the custom page on the default admin site without replacing it.
_django_admin_get_urls = admin.site.get_urls


def _get_urls_with_beta_ops():
    return [
        path("beta-ops/", admin.site.admin_view(beta_ops_dashboard), name="beta_ops"),
    ] + _django_admin_get_urls()


admin.site.get_urls = _get_urls_with_beta_ops
