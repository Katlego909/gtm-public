"""
Tools that give an agent read context and governed write access to the
`dashboard` app's data -- gap analysis, notifications, AI value/impact, and
the workspace teammate roster. Mirrors the shared-tool-builder shape of
`agent_actions.build_agent_action_tools`: a closure-based builder returning
plain functions whose type hints/docstrings become the Gemini tool schema
via automatic function calling.

Gap metrics use the workspace-wins/user-fallback scoping convention already
established by `dashboard.views.helpers._gap_metric_scope_queryset` and
`_upsert_gap_metric_in_scope` (the same convention `escalate_gap` in
agent_actions.py already relies on) -- not the session-wins convention
AgentDocument scoping uses, since these are deliberately different models.

The gap-closure invariant is preserved here exactly as it is for the human
UI: `log_gap_measurement_from_chat` only ever writes a real
GapMetricMeasurement row (tagged source='agent' so it's always traceable to
"a user stated this in chat", never an AI guess) and calls the same
`apply_measured_closure` the dashboard UI's measurement form uses. There is
deliberately no tool for `resolve_gap_metric` (the human-only manual close)
-- that stays UI-only.
"""

from typing import Any, List, Optional


def _check_workspace_permission(workspace, user, permission: Optional[str] = None) -> Optional[str]:
    """None if the action is allowed; otherwise a denial string safe to
    return directly from a tool.

    With no workspace there's no membership to check -- the caller's own
    session-scoped data has no team to gate against, so this only denies
    when a workspace is given. `permission=None` requires only active
    membership (the light bar every write tool gets); a named permission
    (e.g. 'can_assign_tasks') additionally requires that
    WorkspaceMembership property, matching the tier the equivalent human
    view enforces.
    """
    if not workspace:
        return None
    from .models_workspace import WorkspaceMembership

    if not user:
        return "You need to be signed in to do that."
    membership = WorkspaceMembership.objects.filter(
        workspace=workspace, user=user, is_active=True
    ).first()
    if not membership:
        return "You don't have access to this workspace."
    if permission and not getattr(membership, permission, False):
        return "You don't have permission to do that in this workspace."
    return None


def build_dashboard_tools(
    workspace=None,
    session=None,
    user=None,
    task_refs_sink: Optional[List[Any]] = None,
) -> List[Any]:
    """Build the shared set of dashboard-data tools available to any agent
    chatting on behalf of `user`, scoped to `workspace` (if any) and/or
    `session` (if any). Read tools (list_gap_metrics, get_gap_metric_detail,
    read_my_notifications) work with just a session and no workspace, so a
    workspace-less Charlie session still gets gap/notification context.
    Workspace-only tools (get_ai_value_summary, list_teammates) are simply
    omitted from the returned list when there's no workspace, rather than
    included but erroring.
    """
    from dashboard.models import GapAnalysisMetric, GapMetricMeasurement, Notification
    from dashboard.views.helpers import _gap_metric_scope_queryset, apply_measured_closure, prepare_gap_metrics_for_display

    def list_gap_metrics(status: str = "open") -> str:
        """List GTM gap-analysis metrics in this workspace (or, outside a
        workspace, the ones you own). `status` is one of: open, closed, all
        (default open). Each line shows the metric, category, current vs
        target, priority, and how the current value was sourced (measured
        check-in, CRM pull, AI estimate, or manual entry) so you know how
        much to trust it. Use get_gap_metric_detail for one metric's full
        measurement history before logging a new measurement.
        """
        qs = _gap_metric_scope_queryset(user, workspace)
        if status in ("open", "closed"):
            qs = qs.filter(status=status)
        metrics = prepare_gap_metrics_for_display(list(qs.order_by("status", "metric")), workspace, user)
        if not metrics:
            return "No gap metrics found in scope."
        lines = ["Gap metrics:"]
        for m in metrics:
            source_label = m.estimate_method_label or "unspecified"
            lines.append(
                f"- {m.metric} ({m.category}, {m.priority} priority, {m.get_status_display()}): "
                f"current {m.current} vs target {m.target} ({m.gap_percent}), source: {source_label}"
            )
        return "\n".join(lines)

    def get_gap_metric_detail(metric_name: str) -> str:
        """Get one gap metric's full detail, including its 5 most recent
        measurement check-ins. `metric_name` must be one of: Monthly
        Qualified Leads, Average Deal Size, Net Revenue Retention, Product
        Qualified Leads, Win Rate, CAC Payback Period.
        """
        if metric_name not in dict(GapAnalysisMetric.METRIC_CHOICES):
            return f"'{metric_name}' isn't a recognized metric."
        metric = _gap_metric_scope_queryset(user, workspace, metric_name).first()
        if not metric:
            return f"No gap metric found for '{metric_name}' in scope."
        prepare_gap_metrics_for_display([metric], workspace, user)
        lines = [
            f"{metric.metric} ({metric.category}, {metric.priority} priority, {metric.get_status_display()})",
            f"Current: {metric.current}  Target: {metric.target}  Gap: {metric.gap_percent}",
            f"Source: {metric.estimate_method_label or 'unspecified'}",
            f"Recommendation: {metric.recommendation}",
        ]
        measurements = list(metric.measurements.all()[:5])
        if measurements:
            lines.append("Recent measurements:")
            for meas in measurements:
                note_suffix = f" -- {meas.note}" if meas.note else ""
                lines.append(f"- {meas.value} on {meas.measured_at:%Y-%m-%d} ({meas.get_source_display()}){note_suffix}")
        else:
            lines.append("No measurements logged yet.")
        return "\n".join(lines)

    def log_gap_measurement_from_chat(metric_name: str, value: float, note: str = "") -> str:
        """Log a real, measured current value for a gap metric because the
        user told you the actual number in this conversation (e.g. "our
        MQLs are at 450 now") -- NEVER call this with a number you
        estimated or guessed yourself, only a value the user explicitly
        stated. This can auto-close the gap if the value reaches its
        target, exactly like a human logging a check-in in the Gap
        Analysis panel. `metric_name` must be one of: Monthly Qualified
        Leads, Average Deal Size, Net Revenue Retention, Product Qualified
        Leads, Win Rate, CAC Payback Period. Use escalate_gap first if this
        gap hasn't been flagged yet.
        """
        denial = _check_workspace_permission(workspace, user)
        if denial:
            return denial
        if metric_name not in dict(GapAnalysisMetric.METRIC_CHOICES):
            return f"'{metric_name}' isn't a recognized metric."
        metric = _gap_metric_scope_queryset(user, workspace, metric_name).first()
        if not metric:
            return f"No gap metric found for '{metric_name}' in scope -- use escalate_gap first to flag it."

        default_note = f"Reported via chat by {user.get_full_name() or user.username}." if user else "Reported via chat."
        GapMetricMeasurement.objects.create(
            gap_metric=metric, value=value, source="agent",
            note=(note.strip() or default_note)[:240], recorded_by=user,
        )
        metric.current = value
        metric.estimate_method = "measured"
        metric.save(update_fields=["current", "estimate_method"])
        closed = apply_measured_closure(metric)
        if closed:
            return f"Logged {metric.metric} at {value} and closed the gap -- target reached ({metric.target})."
        return f"Logged {metric.metric} at {value}. Target: {metric.target}."

    def read_my_notifications(unread_only: bool = True) -> str:
        """List your (the requesting user's) own notifications -- never
        anyone else's -- such as task assignments, status updates, and
        AI-report-ready alerts. `unread_only` (default True) limits to
        unread ones.
        """
        if not user:
            return "No notifications available -- you're not signed in."
        from django.db.models import Q

        qs = Notification.objects.filter(recipient=user)
        if workspace:
            qs = qs.filter(Q(workspace=workspace) | Q(workspace__isnull=True))
        if unread_only:
            qs = qs.filter(is_read=False)
        notifications = list(qs.order_by("-created_at")[:10])
        if not notifications:
            return "No unread notifications." if unread_only else "No notifications."
        lines = ["Notifications:"]
        for n in notifications:
            unread_flag = " [unread]" if not n.is_read else ""
            lines.append(f"- ({n.get_level_display()}) {n.title}: {n.message[:120]}{unread_flag}")
        return "\n".join(lines)

    def get_ai_value_summary() -> str:
        """Summarize this workspace's real AI agent output and usage --
        documents created, tasks completed by AI, conversations, and
        actual Gemini token cost -- plus a separately-labeled estimate of
        time/cost saved. Use this if asked how much value the AI agents
        are providing.
        """
        from dashboard.agent_value import get_agent_value_context

        context = get_agent_value_context(workspace)
        if not context.get("has_workspace"):
            return "No AI value data available outside a workspace."
        measured = context["measured"]
        estimated = context["estimated"]
        return (
            f"Measured (real data): {measured['documents_total']} documents created "
            f"({measured['documents_this_week']} this week), {measured['tasks_completed']} tasks "
            f"completed by AI, {measured['conversations_total']} conversations, "
            f"${measured['ai_cost_usd']:.4f} actual AI cost.\n"
            f"Estimated (assumption-based, kept separate from the measured numbers above): "
            f"~{estimated['hours_saved']} hours saved, ~${estimated['cost_avoided_usd']:.2f} cost avoided."
        )

    def list_teammates() -> str:
        """List active members of this workspace with their role, so you
        know who to notify, assign a task to, or route a follow-up
        toward."""
        from .models_workspace import WorkspaceMembership

        memberships = WorkspaceMembership.objects.filter(
            workspace=workspace, is_active=True
        ).select_related("user").order_by("user__first_name", "user__username")
        if not memberships:
            return "No active teammates found in this workspace."
        lines = ["Workspace teammates:"]
        for membership in memberships:
            name = membership.user.get_full_name() or membership.user.username
            lines.append(f"- {name} ({membership.get_role_display()})")
        return "\n".join(lines)

    tools = [list_gap_metrics, get_gap_metric_detail, log_gap_measurement_from_chat, read_my_notifications]
    if workspace:
        tools += [get_ai_value_summary, list_teammates]
    return tools


def build_dashboard_ambient_context(*, workspace=None, session=None, user=None) -> str:
    """Cheap, always-computed system-prompt addendum so every agent has
    open-gap and unread-notification awareness without spending a tool
    round-trip on it. Two small aggregate queries, no per-session score
    recomputation (that's what prepare_gap_metrics_for_display is for, via
    the on-demand list_gap_metrics tool -- too heavy to run on every turn).
    Returns "" when there's nothing to report, so a quiet workspace never
    gets prompt bloat.
    """
    from dashboard.models import Notification
    from dashboard.views.helpers import _gap_metric_scope_queryset

    open_gaps = _gap_metric_scope_queryset(user, workspace).filter(status="open")
    open_gap_count = open_gaps.count()
    high_priority_open_gap_count = open_gaps.filter(priority="High").count()

    unread_notification_count = 0
    if user:
        from django.db.models import Q

        notif_qs = Notification.objects.filter(recipient=user, is_read=False)
        if workspace:
            notif_qs = notif_qs.filter(Q(workspace=workspace) | Q(workspace__isnull=True))
        unread_notification_count = notif_qs.count()

    if not open_gap_count and not unread_notification_count:
        return ""

    parts = []
    if open_gap_count:
        parts.append(f"{open_gap_count} open GTM gap(s) ({high_priority_open_gap_count} high priority)")
    if unread_notification_count:
        parts.append(f"{unread_notification_count} unread notification(s) for the current user")
    return "\n\nCurrent dashboard state: " + ", ".join(parts) + ". Use list_gap_metrics/read_my_notifications for detail."
