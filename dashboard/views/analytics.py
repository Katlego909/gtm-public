# Split verbatim from the former monolithic dashboard/views.py. Shared imports
# live in each module's header; shared helpers in dashboard/views/helpers.py.
from ..analytics import get_dashboard_context, _build_assessment_history_export_rows
# dashboard/views.py
"""
Dashboard views for GTM Validator
"""

import csv
import datetime
import json
import mimetypes
import markdown
import os
import re
import uuid
import logging
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Tuple

from django.shortcuts import get_object_or_404, render, redirect
from django.db import models
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, JsonResponse, Http404
from django.utils import timezone
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.vary import vary_on_headers
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.core.mail import send_mail
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.template.loader import render_to_string
from django.utils.text import slugify
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from gtm.models import (
    AssessmentSession,
    ActionItem,
    ActionItemComment,
    ToolRecommendation,
    ResultSnapshot,
    ChatMessage,
    GTMFile,
)
from gtm.models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation, WorkspaceActivityEvent
from gtm.ai_chat import get_suggested_prompts, process_chat_message
from gtm.decorators import workspace_permission_required, workspace_member_required
from dashboard.models import Channel, ChannelAnalytics, GapAnalysisMetric, GapAnalysisSuggestion, Resource, Notification, UserSettings
from dashboard.forms import GapAnalysisMetricForm, ActionItemForm, UserProfileForm, UserSettingsForm
from dashboard.utils_notifications import send_notification
from ..forms import GapAnalysisMetricForm, ActionItemForm, UserProfileForm, ResourceForm
from gtm.views import _compute_scores, _band_for_score
from ..utils import calculate_gap_metric_display_properties

logger = logging.getLogger(__name__)

from ..document_processors import (
    AGENT_ATTACHMENT_MAX_FILES,
    AGENT_ATTACHMENT_MAX_BYTES,
    AGENT_ATTACHMENT_TEXT_LIMIT,
    AGENT_ATTACHMENT_EXCERPT_LIMIT,
    AGENT_ALLOWED_EXTENSIONS,
    AGENT_IMAGE_EXTENSIONS,
    _normalize_content_type,
    _extract_pdf_text,
    _extract_pdf_text_with_ai,
    _extract_image_text_with_ai,
    _extract_attachment_text,
    _save_agent_attachment,
    _process_agent_attachments,
)

# ================================================================

from .helpers import (
    _ai_gap_suggestions_from_assessment,
    _build_sidebar_notifications_context,
    _clean_json_payload,
    _fallback_gap_suggestions,
    _gap_metric_scope_queryset,
    _gap_percent_value,
    _get_gap_scope,
    _load_pending_gap_suggestions,
    _md,
    _resolve_dashboard_workspace,
    _upsert_gap_metric_in_scope,
)

@csrf_exempt
def insight_export(request, pk, fmt):
    """Export an AI insight/playbook as a PDF or Word document."""
    # Only allow access to insights in user's workspace
    workspace_id = request.session.get('current_workspace_id')
    if workspace_id:
        insight = get_object_or_404(ResultSnapshot, pk=pk, session__workspace_id=workspace_id)
    else:
        insight = get_object_or_404(ResultSnapshot, pk=pk, session__user=request.user)

    if fmt == 'pdf':
        from gtm.utils_pdf import render_insight_pdf_response
        return render_insight_pdf_response(company_name=insight.company_name, ai_playbook_md=insight.ai_playbook or '', doc_date=insight.created_at)
    elif fmt == 'docx':
        from gtm.utils_docx import render_insight_docx_response
        return render_insight_docx_response(company_name=insight.company_name, ai_playbook_md=insight.ai_playbook or '', doc_date=insight.created_at)
    else:
        raise Http404("Unsupported export format.")


@login_required
def assessment_history_export(request, fmt):
    """Export the full (unbounded) assessment history for the current workspace
    or, if none is active, the user's own standalone assessments."""
    if fmt != 'csv':
        return HttpResponseBadRequest("Unsupported export format.")

    current_workspace, _ = _resolve_dashboard_workspace(request)
    if current_workspace:
        assessments_qs = AssessmentSession.objects.filter(workspace=current_workspace, is_completed=True)
        scope_slug = current_workspace.slug
    else:
        assessments_qs = AssessmentSession.objects.filter(user=request.user, workspace__isnull=True, is_completed=True)
        scope_slug = request.user.username or 'personal'
    assessments_qs = assessments_qs.select_related('snapshot__band').order_by('-created_at')

    rows = _build_assessment_history_export_rows(assessments_qs)

    filename = f"assessment_history_{scope_slug}_{timezone.now().date().isoformat()}.csv"
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow(['Company', 'Industry', 'Overall Score', 'Stage', 'Assessed On'])
    for row in rows:
        writer.writerow([
            row['company_name'],
            row['industry'],
            row['overall_score'],
            row['band_stage'],
            row['created_at'].date().isoformat(),
        ])

    return response


@csrf_exempt
def insight_feedback(request, pk):
    """Receive feedback for an AI insight."""
    if request.method == 'POST':
        feedback = request.POST.get('feedback', '').strip()
        print(f'Feedback for insight {pk}: {feedback}')
        
        if request.htmx:
            return HttpResponse('<div class="p-4 bg-green-50 text-green-700 rounded-lg text-center">Thanks for your feedback!</div>')
            
        return JsonResponse({'status': 'success', 'message': 'Feedback received.'})
    return JsonResponse({'status': 'error', 'message': 'Invalid request.'}, status=400)


# ================================================================
# KPI VIEWS
# ================================================================

def kpi_total_sessions(request):
    """Return total sessions as an HTML fragment."""
    if request.user.is_authenticated:
        workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
        if workspace_id:
            total_sessions = AssessmentSession.objects.filter(workspace_id=workspace_id).count()
        else:
            total_sessions = AssessmentSession.objects.filter(user=request.user).count()
    else:
        total_sessions = 0
    html = f'<div id="total-sessions">{total_sessions}</div>'
    return HttpResponse(html)


def kpi_completed_items(request):
    """Return completed action items count as an HTML fragment."""
    if request.user.is_authenticated:
        workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
        if workspace_id:
            completed_items = ActionItem.objects.filter(workspace_id=workspace_id, status='done').count()
        else:
            completed_items = ActionItem.objects.filter(session__user=request.user, status='done').count()
    else:
        completed_items = 0
    html = f'<div id="completed-items">{completed_items}</div>'
    return HttpResponse(html)


def kpi_pending_items(request):
    """Return pending action items count as an HTML fragment."""
    if request.user.is_authenticated:
        workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
        if workspace_id:
            pending_items = ActionItem.objects.filter(workspace_id=workspace_id).exclude(status='done').count()
        else:
            pending_items = ActionItem.objects.filter(session__user=request.user).exclude(status='done').count()
    else:
        pending_items = 0
    html = f'<div id="pending-items">{pending_items}</div>'
    return HttpResponse(html)


# ================================================================
# DASHBOARD VIEW
# ================================================================

@vary_on_headers('HX-Request')
@login_required
def analytics(request):
    current_workspace, user_workspaces = _resolve_dashboard_workspace(request)
    agent_session_id = request.GET.get('session_id') or (request.session.get('dashboard_agent_session_id') if request.session else None)
    ctx = get_dashboard_context(request, current_workspace, user_workspaces, agent_session_id)

    # Add gap analysis aggregations (reusing gap_report logic)
    gap_metrics = ctx.get('gap_analysis', [])

    # Calculate priority counts and category summary
    priority_counts = {'High': 0, 'Medium': 0, 'Low': 0}
    category_summary = {}
    at_risk_metrics = []
    source_breakdown = {'AI': 0, 'USER': 0}

    for metric in gap_metrics:
        priority_counts[metric.priority] = priority_counts.get(metric.priority, 0) + 1

        category = metric.category
        if category not in category_summary:
            category_summary[category] = {'count': 0, 'behind_count': 0}
        category_summary[category]['count'] += 1

        source_breakdown[metric.source] = source_breakdown.get(metric.source, 0) + 1

        # Track at-risk metrics (those with negative gaps)
        if hasattr(metric, 'gap_percent'):
            try:
                gap_val = float(str(metric.gap_percent).rstrip('%'))
                if gap_val < 0:
                    at_risk_metrics.append(metric)
            except (ValueError, TypeError):
                pass

    # Sort at-risk metrics by gap percentage (worst first)
    def get_gap_value(m):
        try:
            return float(str(getattr(m, 'gap_percent', 0)).rstrip('%'))
        except (ValueError, TypeError):
            return 0

    at_risk_metrics.sort(key=get_gap_value)
    at_risk_metrics = at_risk_metrics[:6]

    ctx.update({
        'priority_counts': priority_counts,
        'category_summary': list(category_summary.items()),
        'at_risk_metrics': at_risk_metrics,
        'source_breakdown': source_breakdown,
        'page_title': 'Analytics',
    })

    template = 'dashboard/partials/analytics_content.html' if request.htmx else 'dashboard/analytics.html'
    return render(request, template, ctx)


