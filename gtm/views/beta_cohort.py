# gtm/views/beta_cohort.py
"""
Minimal cross-tenant operator view for the beta: "who signed up and is
anyone stuck." Deliberately a plain sortable HTML table, not a full
analytics product -- dashboard/analytics.py and dashboard/agent_value.py
are both hard-scoped to a single workspace, and Django admin only supports
one-record-at-a-time lookups, so there was nothing to reuse here.
"""
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth import get_user_model
from django.shortcuts import render

from ..models import AssessmentSession
from ..models_workspace import WorkspaceMembership

User = get_user_model()


@staff_member_required
def beta_cohort(request):
    rows = []
    users = User.objects.all().order_by("-date_joined").prefetch_related("gtm_sessions")
    memberships = (
        WorkspaceMembership.objects.filter(is_active=True)
        .select_related("workspace", "workspace__ai_credit_account")
    )
    memberships_by_user = {}
    for m in memberships:
        memberships_by_user.setdefault(m.user_id, []).append(m)

    # Simple per-user count via a plain loop -- beta-cohort sized, no need
    # for a heavier annotate/aggregate query here.
    completed_counts = {}
    for user_id in AssessmentSession.objects.filter(is_completed=True, user_id__isnull=False).values_list("user_id", flat=True):
        completed_counts[user_id] = completed_counts.get(user_id, 0) + 1

    for user in users:
        user_memberships = memberships_by_user.get(user.id, [])
        workspace_labels = [f"{m.workspace.name} ({m.role})" for m in user_memberships]

        usage_labels = []
        for m in user_memberships:
            account = getattr(m.workspace, "ai_credit_account", None)
            if account:
                remaining, _ = account.remaining_tokens()
                used_pct = round(100 * (1 - remaining / account.token_budget), 1) if account.token_budget else 0
                usage_labels.append(f"{m.workspace.name}: {used_pct}%")

        rows.append({
            "user": user,
            "workspaces": workspace_labels or ["—"],
            "ai_usage": usage_labels or ["—"],
            "completed_assessments": completed_counts.get(user.id, 0),
        })

    return render(request, "gtm/beta_cohort.html", {"rows": rows})
