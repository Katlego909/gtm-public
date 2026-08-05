"""Service layer for workspace-scoped agent workflows: portfolio trends,
resource-gap matching, and the insights digest. Kept separate from
agent_services.py, which is scoped to a single AssessmentSession.

These functions are deliberately queryset-heavy but N+1-safe: aggregation
across a workspace's assessments always reads from the already-computed
ResultSnapshot.category_breakdown rather than recomputing scores per
session in a loop.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from django.utils import timezone

from .models import ActionItem, ResultSnapshot
from .models_workspace import Workspace, WorkspaceActivityEvent


def get_workspace_snapshots(workspace: Workspace) -> List[ResultSnapshot]:
    """Fetch one workspace's completed-session snapshots, oldest first, in a single query."""
    return list(
        ResultSnapshot.objects.filter(
            session__workspace=workspace, session__is_completed=True
        )
        .select_related("session")
        .order_by("session__created_at")
    )


def build_portfolio_trends(
    workspace: Workspace,
    snapshots: Optional[List[ResultSnapshot]] = None,
) -> Dict[str, Any]:
    """Aggregate score trends across all of a workspace's completed assessments."""
    if snapshots is None:
        snapshots = get_workspace_snapshots(workspace)

    sessions = [
        {
            "uuid": str(snap.session.uuid),
            "created_at": snap.session.created_at,
            "overall": snap.overall,
            "band_stage": snap.band_stage,
            "category_breakdown": snap.category_breakdown or [],
        }
        for snap in snapshots
    ]

    category_deltas: List[Dict[str, Any]] = []
    overall_delta = None
    if len(sessions) >= 2:
        first_categories = {row["category"]: row["avg"] for row in sessions[0]["category_breakdown"]}
        latest_categories = {row["category"]: row["avg"] for row in sessions[-1]["category_breakdown"]}
        for category, latest_avg in latest_categories.items():
            first_avg = first_categories.get(category)
            if first_avg is None:
                continue
            category_deltas.append({
                "category": category,
                "first": round(first_avg, 2),
                "latest": round(latest_avg, 2),
                "delta": round(latest_avg - first_avg, 2),
            })
        category_deltas.sort(key=lambda row: row["delta"])
        overall_delta = round(sessions[-1]["overall"] - sessions[0]["overall"], 1)

    latest_categories_sorted = (
        sorted(sessions[-1]["category_breakdown"], key=lambda row: row.get("avg", 0))
        if sessions else []
    )

    return {
        "session_count": len(sessions),
        "sessions": sessions,
        "latest_session_uuid": sessions[-1]["uuid"] if sessions else None,
        "latest_overall": sessions[-1]["overall"] if sessions else None,
        "overall_delta": overall_delta,
        "category_deltas": category_deltas,
        "weakest_latest_categories": [row["category"] for row in latest_categories_sorted[:3]],
    }


def build_resource_gap_matches(workspace: Workspace) -> Dict[str, Any]:
    """Cross-reference the workspace's persistently-weak categories against its Resource library."""
    from dashboard.models import AIResourceRecommendation, Resource

    trends = build_portfolio_trends(workspace)
    weakest_categories = trends["weakest_latest_categories"]

    resources = list(Resource.objects.filter(workspace=workspace).order_by("-created_at"))
    already_recommended = set(
        AIResourceRecommendation.objects.filter(session__workspace=workspace)
        .select_related("resource")
        .values_list("resource_id", flat=True)
    )

    unrecommended = [r for r in resources if r.id not in already_recommended]

    # A GTM category has no library coverage if there is no Resource in a
    # roughly matching CATEGORIES bucket. This is a heuristic, not an exact
    # taxonomy match (GTM category names and Resource.CATEGORIES differ).
    resource_categories = {r.category for r in resources}
    coverage_gaps = [
        category for category in weakest_categories
        if category.lower() not in resource_categories and not any(
            category.lower() in rc for rc in resource_categories
        )
    ]

    return {
        "weakest_categories": weakest_categories,
        "resource_count": len(resources),
        "resources": resources,
        "unrecommended_resources": unrecommended,
        "coverage_gaps": coverage_gaps,
    }


def build_workspace_insights_digest(workspace: Workspace, user=None, persist: bool = True) -> Dict[str, Any]:
    """Aggregate recent activity, score deltas, and pending review items into a digest.

    `persist` controls whether callers intend to save the digest text as a
    WorkspaceChatMessage — this function itself only computes the data, it
    does not write to the DB (persistence is the caller's responsibility so
    this stays testable without hitting the chat table).

    `user`, when given, adds that user's own unread-notification count.
    Omit it for a workspace-wide broadcast digest (e.g. the scheduled
    refresh notified out to several recipients) where a single user's
    inbox count wouldn't mean anything to the group.
    """
    from dashboard.models import GapAnalysisSuggestion, Notification
    from dashboard.views.helpers import _gap_metric_scope_queryset

    trends = build_portfolio_trends(workspace)

    recent_events = list(
        WorkspaceActivityEvent.objects.filter(workspace=workspace)
        .select_related("actor")
        .order_by("-created_at")[:50]
    )

    pending_suggestions = list(
        GapAnalysisSuggestion.objects.filter(workspace=workspace, status="pending")
        .order_by("-created_at")
    )

    open_actions = ActionItem.objects.filter(workspace=workspace).exclude(status="done")
    today = timezone.localdate()
    overdue_actions = list(open_actions.filter(due_date__lt=today).select_related("assigned_to"))
    open_count = open_actions.count()

    open_gaps = _gap_metric_scope_queryset(user, workspace).filter(status="open")
    open_gap_count = open_gaps.count()
    high_priority_open_gap_count = open_gaps.filter(priority="High").count()
    unread_notification_count = (
        Notification.objects.filter(recipient=user, workspace=workspace, is_read=False).count() if user else 0
    )

    return {
        "trends": trends,
        "recent_events": recent_events,
        "pending_suggestions": pending_suggestions,
        "pending_suggestion_count": len(pending_suggestions),
        "open_action_count": open_count,
        "overdue_actions": overdue_actions,
        "overdue_action_count": len(overdue_actions),
        "open_gap_count": open_gap_count,
        "high_priority_open_gap_count": high_priority_open_gap_count,
        "unread_notification_count": unread_notification_count,
        "generated_at": timezone.now(),
    }
