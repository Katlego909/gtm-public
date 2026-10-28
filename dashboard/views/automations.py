"""Automations panel: see what keeps running in this workspace, and pause,
resume, or delete it. Only people who can approve agent plans can change an
automation, since agents create them through approved plans."""

from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from gtm.automations import describe
from gtm.models import Automation
from gtm.models_workspace import Workspace, WorkspaceMembership


def _workspace_and_membership(request):
    workspace_id = request.GET.get("workspace") or request.POST.get("workspace") or request.session.get("current_workspace_id")
    workspace = Workspace.objects.filter(id=workspace_id).first() if workspace_id else None
    membership = (
        WorkspaceMembership.objects.filter(user=request.user, workspace=workspace, is_active=True).first()
        if workspace else None
    )
    if membership is None:
        raise Http404
    return workspace, membership


def _render(request, workspace, membership, error=""):
    automations = list(
        Automation.objects.filter(workspace=workspace).select_related("created_by_run", "approved_by").prefetch_related("runs")
    )
    rows = [{"a": a, "summary": describe(a), "last_run": next(iter(a.runs.all()), None)} for a in automations]
    return render(request, "dashboard/partials/automations_panel.html", {
        "workspace": workspace,
        "rows": rows,
        "can_manage": membership.can_approve_agent_runs,
        "error": error,
    })


@login_required
def automations_panel(request):
    workspace, membership = _workspace_and_membership(request)
    return _render(request, workspace, membership)


@login_required
@require_POST
def automation_toggle(request, automation_id):
    workspace, membership = _workspace_and_membership(request)
    automation = get_object_or_404(Automation, pk=automation_id, workspace=workspace)
    if not membership.can_approve_agent_runs:
        return _render(request, workspace, membership, error="You don't have permission to change automations.")
    automation.is_active = not automation.is_active
    automation.save(update_fields=["is_active"])
    return _render(request, workspace, membership)


@login_required
@require_POST
def automation_delete(request, automation_id):
    workspace, membership = _workspace_and_membership(request)
    automation = get_object_or_404(Automation, pk=automation_id, workspace=workspace)
    if not membership.can_approve_agent_runs:
        return _render(request, workspace, membership, error="You don't have permission to delete automations.")
    automation.delete()
    return _render(request, workspace, membership)
