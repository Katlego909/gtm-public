from django.contrib.auth.decorators import login_required
from django.db import models
from django.shortcuts import render
from django.views.decorators.http import require_http_methods

from gtm.models import ActionItem, AgentDocument, AssessmentSession
from dashboard.models import Resource

from .helpers import _resolve_dashboard_workspace

RESULTS_PER_CATEGORY = 5
MIN_QUERY_LENGTH = 2


@require_http_methods(["GET"])
@login_required
def global_search(request):
    """Type-ahead search across Assessments, Tasks, Resources, and Documents,
    scoped to the current workspace (or the user's personal assessments/tasks
    when no workspace is selected)."""
    q = request.GET.get('q', '').strip()
    if len(q) < MIN_QUERY_LENGTH:
        return render(request, 'dashboard/partials/search_results.html', {'query': q, 'too_short': True})

    current_workspace, _ = _resolve_dashboard_workspace(request)

    if current_workspace:
        assessments = AssessmentSession.objects.filter(
            workspace=current_workspace, company_name__icontains=q
        ).order_by('-created_at')[:RESULTS_PER_CATEGORY]
        tasks = ActionItem.objects.filter(
            workspace=current_workspace, note__icontains=q
        ).order_by('-updated_at')[:RESULTS_PER_CATEGORY]
        resources = Resource.objects.filter(workspace=current_workspace).filter(
            models.Q(name__icontains=q) | models.Q(description__icontains=q)
        ).order_by('-created_at')[:RESULTS_PER_CATEGORY]
        documents = AgentDocument.objects.filter(workspace=current_workspace).filter(
            models.Q(title__icontains=q) | models.Q(content__icontains=q)
        ).order_by('-updated_at')[:RESULTS_PER_CATEGORY]
    else:
        assessments = AssessmentSession.objects.filter(
            user=request.user, workspace__isnull=True, company_name__icontains=q
        ).order_by('-created_at')[:RESULTS_PER_CATEGORY]
        tasks = ActionItem.objects.filter(
            session__user=request.user, workspace__isnull=True, note__icontains=q
        ).order_by('-updated_at')[:RESULTS_PER_CATEGORY]
        resources = Resource.objects.none()
        documents = AgentDocument.objects.none()

    return render(request, 'dashboard/partials/search_results.html', {
        'query': q,
        'current_workspace': current_workspace,
        'assessments': assessments,
        'tasks': tasks,
        'resources': resources,
        'documents': documents,
    })
