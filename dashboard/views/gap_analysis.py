# Split verbatim from the former monolithic dashboard/views.py. Shared imports
# live in each module's header; shared helpers in dashboard/views/helpers.py.
from ..analytics import get_dashboard_context
# dashboard/views.py
"""
Dashboard views for GTM Validator
"""

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
from django.http import HttpResponse, JsonResponse, Http404
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
from dashboard.models import Channel, ChannelAnalytics, GapAnalysisMetric, GapAnalysisSuggestion, GapMetricMeasurement, Resource, Notification, UserSettings
from dashboard.forms import GapAnalysisMetricForm, GapMeasurementForm, ActionItemForm, UserProfileForm, UserSettingsForm
from dashboard.utils_notifications import send_notification
from ..forms import GapAnalysisMetricForm, ActionItemForm, UserProfileForm, ResourceForm
from gtm.views import _compute_scores, _band_for_score
from ..utils import calculate_gap_metric_display_properties
from ..parsers import log_workspace_activity

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
    _ai_action_items_for_gap_metric,
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
    apply_measured_closure,
    prepare_gap_metrics_for_display,
)

@require_http_methods(["GET"])
@login_required
def refresh_gap_suggestions(request):
    current_workspace, latest_completed_session = _get_gap_scope(request)
    suggestions = _load_pending_gap_suggestions(request, current_workspace)
    return render(request, 'dashboard/partials/gap_suggestions_panel.html', {
        'gap_suggestions': suggestions,
        'current_workspace': current_workspace,
        'latest_completed_session': latest_completed_session,
    })


@require_http_methods(["POST"])
@login_required
def generate_gap_suggestions(request):
    current_workspace, latest_completed_session = _get_gap_scope(request)
    if not latest_completed_session:
        response = render(request, 'dashboard/partials/gap_suggestions_panel.html', {
            'gap_suggestions': [],
            'current_workspace': current_workspace,
            'latest_completed_session': None,
            'suggestions_error': 'No completed assessment found for this scope yet.',
        })
        response['HX-Trigger'] = json.dumps({
            'resourceToast': {
                'message': 'No completed assessment found yet.',
                'level': 'warning',
            }
        })
        return response

    suggestions_data, metadata = _ai_gap_suggestions_from_assessment(latest_completed_session)

    with transaction.atomic():
        existing_qs = GapAnalysisSuggestion.objects.filter(
            user=request.user,
            status='pending',
            workspace=current_workspace,
        ) if current_workspace else GapAnalysisSuggestion.objects.filter(
            user=request.user,
            status='pending',
            workspace__isnull=True,
        )
        existing_qs.delete()

        GapAnalysisSuggestion.objects.bulk_create([
            GapAnalysisSuggestion(
                category=item['category'],
                metric=item['metric'],
                current=item['current'],
                target=item['target'],
                priority=item['priority'],
                recommendation=item['recommendation'],
                rationale=item.get('rationale', ''),
                confidence=item.get('confidence', 70),
                status='pending',
                session=latest_completed_session,
                workspace=current_workspace,
                user=request.user,
                source_payload=metadata,
            )
            for item in suggestions_data
        ])

    suggestions = _load_pending_gap_suggestions(request, current_workspace)
    response = render(request, 'dashboard/partials/gap_suggestions_panel.html', {
        'gap_suggestions': suggestions,
        'current_workspace': current_workspace,
        'latest_completed_session': latest_completed_session,
    })
    response['HX-Trigger'] = json.dumps({
        'resourceToast': {
            'message': f'Generated {len(suggestions)} AI suggestion(s). Review and accept what fits.',
            'level': 'success',
        }
    })
    return response


@require_http_methods(["POST"])
@login_required
def accept_gap_suggestion(request, suggestion_id):
    current_workspace, _ = _get_gap_scope(request)
    suggestion_qs = GapAnalysisSuggestion.objects.filter(id=suggestion_id, user=request.user, status='pending')
    if current_workspace:
        suggestion_qs = suggestion_qs.filter(workspace=current_workspace)
    else:
        suggestion_qs = suggestion_qs.filter(workspace__isnull=True)
    suggestion = get_object_or_404(suggestion_qs)

    with transaction.atomic():
        _upsert_gap_metric_in_scope(
            user=request.user,
            workspace=suggestion.workspace,
            session=suggestion.session,
            payload={
                'category': suggestion.category,
                'metric': suggestion.metric,
                'current': suggestion.current,
                'target': suggestion.target,
                'priority': suggestion.priority,
                'recommendation': suggestion.recommendation,
            },
            source='AI',
        )
        suggestion.status = 'accepted'
        suggestion.save(update_fields=['status', 'updated_at'])

    suggestions = _load_pending_gap_suggestions(request, current_workspace)
    response = render(request, 'dashboard/partials/gap_suggestions_panel.html', {
        'gap_suggestions': suggestions,
        'current_workspace': current_workspace,
        'latest_completed_session': suggestion.session,
    })
    response['HX-Trigger'] = json.dumps({
        'gapAnalysisUpdated': True,
        'resourceToast': {
            'message': f"Accepted AI suggestion for '{suggestion.metric}'.",
            'level': 'success',
        }
    })
    return response


@require_http_methods(["POST"])
@login_required
def generate_action_items_for_gap(request, pk):
    """Turn one accepted GapAnalysisMetric's standing recommendation into
    real ActionItems via one AI call, so it can flow through the existing
    Tasks board / Complete-with-AI pipeline instead of sitting as a static
    row with no path to action."""
    current_workspace, latest_completed_session = _get_gap_scope(request)
    metric_qs = GapAnalysisMetric.objects.filter(workspace=current_workspace) if current_workspace \
        else GapAnalysisMetric.objects.filter(user=request.user, workspace__isnull=True)
    metric = get_object_or_404(metric_qs, pk=pk)

    action_texts, _metadata = _ai_action_items_for_gap_metric(metric)
    session = metric.session or latest_completed_session

    created = []
    for text in action_texts:
        item, was_created = ActionItem.objects.create_deduped(
            session=session,
            workspace=current_workspace,
            note=text,
            status='todo',
            created_by=request.user,
            owner=request.user.get_full_name() or request.user.username,
            gap_metric=metric,
        )
        if was_created:
            created.append(item)
        elif item is not None and item.gap_metric_id is None:
            # A note-identical task created some other way still counts as
            # remediation for this gap -- link it so progress tracks it.
            item.gap_metric = metric
            item.save(update_fields=['gap_metric'])

    for item in created:
        log_workspace_activity(
            current_workspace,
            request.user,
            'task_created',
            f"AI created task '{item.note[:80]}' to close the {metric.metric} gap.",
            object_type='action_item',
            object_id=item.id,
            metadata={'source': 'gap_remediation', 'gap_metric_id': metric.id},
            session=item.session,
        )

    if created:
        toast_message = f"Created {len(created)} action item(s) to close the {metric.metric} gap. Find them on the Tasks board."
    else:
        toast_message = f"Those action items already exist for the {metric.metric} gap."

    response = HttpResponse(status=204)
    response['HX-Trigger'] = json.dumps({
        'gapAnalysisUpdated': True,
        'refreshAgentActions': True,
        'refreshNotifications': True,
        'resourceToast': {
            'message': toast_message,
            'level': 'success',
        }
    })
    return response


def _get_scoped_gap_metric_or_404(request, pk):
    """Shared scope check for per-metric actions: workspace members act on
    workspace metrics, otherwise only the owner's unscoped metrics."""
    current_workspace, latest_completed_session = _get_gap_scope(request)
    metric_qs = GapAnalysisMetric.objects.filter(workspace=current_workspace) if current_workspace \
        else GapAnalysisMetric.objects.filter(user=request.user, workspace__isnull=True)
    return get_object_or_404(metric_qs, pk=pk), current_workspace, latest_completed_session


def _render_gap_metric_row(request, metric, current_workspace):
    prepare_gap_metrics_for_display([metric], current_workspace, request.user)
    return render(request, 'dashboard/partials/_gap_analysis_row.html', {'gap': metric})


@login_required
def log_gap_measurement(request, pk):
    """Record a real, measured current value for a gap metric. This is the
    only path (besides the future CRM pull) that can auto-close a gap:
    measured data reaching target, never task completion or AI estimates."""
    metric, current_workspace, _ = _get_scoped_gap_metric_or_404(request, pk)

    if request.method == 'POST':
        form = GapMeasurementForm(request.POST)
        if form.is_valid():
            value = form.cleaned_data['value']
            GapMetricMeasurement.objects.create(
                gap_metric=metric,
                value=value,
                source='manual',
                note=form.cleaned_data.get('note', ''),
                recorded_by=request.user,
            )
            metric.current = value
            metric.save(update_fields=['current'])
            closed = apply_measured_closure(metric)

            response = _render_gap_metric_row(request, metric, current_workspace)
            toast = f"Gap closed: {metric.metric} reached its target ({value} vs {metric.target})." if closed \
                else f"Logged {metric.metric} at {value}. Target: {metric.target}."
            response['HX-Trigger'] = json.dumps({
                'closeModal': True,
                'resourceToast': {'message': toast, 'level': 'success'},
            })
            response['HX-Retarget'] = f"#gap-metric-{metric.id}"
            response['HX-Reswap'] = 'outerHTML'
            return response
    else:
        form = GapMeasurementForm()

    return render(request, 'dashboard/partials/gap_measurement_form.html', {
        'form': form,
        'metric': metric,
        'measurements': metric.measurements.all()[:5],
    })


@require_http_methods(["POST"])
@login_required
def resolve_gap_metric(request, pk):
    """Manually mark a gap resolved -- the human confirms the outcome; the
    system never claims closure on its own without measured data."""
    metric, current_workspace, _ = _get_scoped_gap_metric_or_404(request, pk)
    metric.status = 'closed'
    metric.closed_reason = 'manual'
    metric.closed_at = timezone.now()
    metric.save(update_fields=['status', 'closed_reason', 'closed_at'])
    return _render_gap_metric_row(request, metric, current_workspace)


@require_http_methods(["POST"])
@login_required
def reopen_gap_metric(request, pk):
    metric, current_workspace, _ = _get_scoped_gap_metric_or_404(request, pk)
    metric.status = 'open'
    metric.closed_reason = ''
    metric.closed_at = None
    metric.save(update_fields=['status', 'closed_reason', 'closed_at'])
    return _render_gap_metric_row(request, metric, current_workspace)


@require_http_methods(["POST"])
@login_required
def reject_gap_suggestion(request, suggestion_id):
    current_workspace, _ = _get_gap_scope(request)
    suggestion_qs = GapAnalysisSuggestion.objects.filter(id=suggestion_id, user=request.user, status='pending')
    if current_workspace:
        suggestion_qs = suggestion_qs.filter(workspace=current_workspace)
    else:
        suggestion_qs = suggestion_qs.filter(workspace__isnull=True)
    suggestion = get_object_or_404(suggestion_qs)
    suggestion.status = 'rejected'
    suggestion.save(update_fields=['status', 'updated_at'])

    suggestions = _load_pending_gap_suggestions(request, current_workspace)
    response = render(request, 'dashboard/partials/gap_suggestions_panel.html', {
        'gap_suggestions': suggestions,
        'current_workspace': current_workspace,
        'latest_completed_session': suggestion.session,
    })
    response['HX-Trigger'] = json.dumps({
        'resourceToast': {
            'message': f"Rejected AI suggestion for '{suggestion.metric}'.",
            'level': 'info',
        }
    })
    return response


@workspace_member_required('session')
@vary_on_headers('HX-Request')
def add_edit_gap_metric(request, pk=None):
    # Get current workspace context
    workspace_id = request.session.get('current_workspace_id')
    current_workspace = None
    if workspace_id:
        try:
            from gtm.models_workspace import Workspace
            current_workspace = Workspace.objects.get(id=workspace_id)
        except Workspace.DoesNotExist:
            pass
    
    if pk:
        if current_workspace:
            instance = get_object_or_404(GapAnalysisMetric, pk=pk, workspace=current_workspace)
            # Only the creator can edit
            if instance.user is not None and instance.user != request.user:
                if request.htmx:
                    return render(request, 'dashboard/partials/error_modal.html', {
                        'title': 'Access Denied',
                        'message': 'You can only edit metrics that you created.'
                    })
                messages.error(request, 'You can only edit your own metrics.')
                return redirect('dashboard')
        else:
            instance = get_object_or_404(GapAnalysisMetric, pk=pk, user=request.user, workspace__isnull=True)
        title = "Edit Gap Metric"
    else:
        instance = None
        title = "Add Gap Metric"

    if request.method == 'POST':
        form = GapAnalysisMetricForm(request.POST, instance=instance)
        if form.is_valid():
            if instance and instance.pk:
                instance = form.save(commit=False)
                instance.save()
            else:
                cleaned = form.cleaned_data
                instance, _ = _upsert_gap_metric_in_scope(
                    user=request.user,
                    workspace=current_workspace,
                    session=getattr(instance, 'session', None),
                    payload={
                        'category': cleaned['category'],
                        'metric': cleaned['metric'],
                        'current': cleaned['current'],
                        'target': cleaned['target'],
                        'priority': cleaned['priority'],
                        'recommendation': cleaned['recommendation'],
                    },
                    source='USER',
                )
            
            if request.htmx:
                # Return the updated gap analysis table and close modal
                if current_workspace:
                    gap_analysis = GapAnalysisMetric.objects.filter(
                        workspace=current_workspace
                    ).order_by('category', 'priority')
                else:
                    gap_analysis = GapAnalysisMetric.objects.filter(
                        user=request.user, workspace__isnull=True
                    ).order_by('category', 'priority')
                gap_analysis = prepare_gap_metrics_for_display(gap_analysis, current_workspace, request.user)
                response = HttpResponse(render(request, 'dashboard/partials/gap_analysis_table.html', {'gap_analysis': gap_analysis}).content)
                response['HX-Trigger'] = 'closeModal'
                return response
            return redirect('dashboard')
    else:
        form = GapAnalysisMetricForm(instance=instance)

    context = {
        'form': form,
        'title': title,
        'instance': instance
    }
    
    template = 'dashboard/partials/gap_metric_form.html' if request.htmx else 'dashboard/gap_metric_form.html'
    return render(request, template, context)


@workspace_member_required('session')
@vary_on_headers('HX-Request')
def delete_gap_metric(request, pk):
    # Get current workspace context
    workspace_id = request.session.get('current_workspace_id')
    current_workspace = None
    if workspace_id:
        try:
            current_workspace = Workspace.objects.get(id=workspace_id)
        except Workspace.DoesNotExist:
            pass
    
    # First, check if the metric exists in the workspace
    if current_workspace:
        try:
            metric = GapAnalysisMetric.objects.get(
                pk=pk, 
                workspace=current_workspace
            )
        except GapAnalysisMetric.DoesNotExist:
            if request.htmx:
                return render(request, 'dashboard/partials/error_row.html', {
                    'pk': pk,
                    'message': 'This metric no longer exists.',
                })
            raise Http404
        
        # Only the creator can delete (user=None means no owner recorded yet, claim it)
        if metric.user is None:
            metric.user = request.user
            metric.save(update_fields=['user'])
        elif metric.user != request.user:
            if request.htmx:
                return render(request, 'dashboard/partials/error_row.html', {
                    'pk': pk,
                    'message': 'You can only delete metrics that you created.',
                    'restore_url': reverse('get_gap_metric_row', kwargs={'pk': pk}),
                })
            messages.error(request, 'You can only delete your own gap analysis metrics.')
            return redirect('dashboard')
            
        instance = metric
    else:
        try:
            instance = GapAnalysisMetric.objects.get(
                pk=pk, 
                user=request.user, 
                workspace__isnull=True
            )
        except GapAnalysisMetric.DoesNotExist:
            if request.htmx:
                return render(request, 'dashboard/partials/error_row.html', {
                    'pk': pk,
                    'message': 'This metric no longer exists.',
                })
            raise Http404

    if request.method == 'POST':
        instance.delete()
        if request.htmx:
            return HttpResponse('')
        return redirect('dashboard')

    # Show inline delete confirmation (replaces the row)
    context = {'instance': instance}
    return render(request, 'dashboard/partials/_gap_metric_confirm_delete_inline.html', context)
# ================================================================
# INSIGHT VIEWS
# ================================================================

def gap_analysis_table(request):
    workspace_id = request.GET.get('workspace') or request.POST.get('workspace') or request.session.get('current_workspace_id')
    if workspace_id:
        gap_analysis = GapAnalysisMetric.objects.filter(
            workspace_id=workspace_id
        )
    else:
        gap_analysis = GapAnalysisMetric.objects.filter(
            user=request.user,
            workspace__isnull=True,
        )
    gap_analysis = prepare_gap_metrics_for_display(gap_analysis, workspace_id, request.user)

    return render(request, 'dashboard/partials/gap_analysis_table.html', {'gap_analysis': gap_analysis})


@vary_on_headers('HX-Request')
@login_required
def gap_report(request):
    """Comprehensive detailed gap report page."""
    current_workspace, user_workspaces = _resolve_dashboard_workspace(request)

    if current_workspace:
        metrics_qs = GapAnalysisMetric.objects.filter(workspace=current_workspace).order_by('category', 'priority', 'metric')
        suggestions_qs = GapAnalysisSuggestion.objects.filter(workspace=current_workspace, user=request.user)
        assessment_scope = AssessmentSession.objects.filter(workspace=current_workspace)
    else:
        metrics_qs = GapAnalysisMetric.objects.filter(user=request.user, workspace__isnull=True).order_by('category', 'priority', 'metric')
        suggestions_qs = GapAnalysisSuggestion.objects.filter(user=request.user, workspace__isnull=True)
        assessment_scope = AssessmentSession.objects.filter(user=request.user)

    metrics = prepare_gap_metrics_for_display(metrics_qs, current_workspace, request.user)
    for metric in metrics:
        metric.gap_numeric = _gap_percent_value(metric)
        metric.is_behind = metric.gap_numeric < 0

    total_metrics = len(metrics)
    behind_metrics = [m for m in metrics if m.is_behind]
    on_track_metrics = [m for m in metrics if not m.is_behind]

    avg_gap = round(sum(m.gap_numeric for m in metrics) / total_metrics, 1) if total_metrics else 0.0
    high_priority_count = sum(1 for m in metrics if m.priority == 'High')
    medium_priority_count = sum(1 for m in metrics if m.priority == 'Medium')
    low_priority_count = sum(1 for m in metrics if m.priority == 'Low')

    at_risk_metrics = sorted(behind_metrics, key=lambda m: m.gap_numeric)[:6]

    category_summary = []
    categories = sorted({m.category for m in metrics})
    for category in categories:
        cat_metrics = [m for m in metrics if m.category == category]
        cat_behind = sum(1 for m in cat_metrics if m.is_behind)
        cat_avg = round(sum(m.gap_numeric for m in cat_metrics) / len(cat_metrics), 1) if cat_metrics else 0.0
        category_summary.append({
            'category': category,
            'count': len(cat_metrics),
            'behind_count': cat_behind,
            'avg_gap': cat_avg,
        })

    source_breakdown = {
        'ai': sum(1 for m in metrics if m.source == 'AI'),
        'user': sum(1 for m in metrics if m.source == 'USER'),
    }

    suggestions_breakdown = {
        'pending': suggestions_qs.filter(status='pending').count(),
        'accepted': suggestions_qs.filter(status='accepted').count(),
        'rejected': suggestions_qs.filter(status='rejected').count(),
    }

    completed_assessments = assessment_scope.filter(is_completed=True).count()
    latest_assessment = assessment_scope.filter(is_completed=True).order_by('-created_at').first()

    context = {
        'current_workspace': current_workspace,
        'user_workspaces': user_workspaces,
        'page_title': 'Gap Report',
        'metrics': metrics,
        'total_metrics': total_metrics,
        'behind_count': len(behind_metrics),
        'on_track_count': len(on_track_metrics),
        'avg_gap': avg_gap,
        'high_priority_count': high_priority_count,
        'medium_priority_count': medium_priority_count,
        'low_priority_count': low_priority_count,
        'at_risk_metrics': at_risk_metrics,
        'category_summary': category_summary,
        'source_breakdown': source_breakdown,
        'suggestions_breakdown': suggestions_breakdown,
        'completed_assessments': completed_assessments,
        'latest_assessment': latest_assessment,
    }
    if request.htmx:
        return render(request, 'dashboard/partials/gap_report_content.html', context)
    return render(request, 'dashboard/gap_report.html', context)

def get_gap_metric_row(request, pk):
    # Only allow access to metrics in user's workspace or user-created metrics
    workspace_id = request.GET.get('workspace') or request.POST.get('workspace') or request.session.get('current_workspace_id')
    if workspace_id:
        metric = get_object_or_404(
            GapAnalysisMetric.objects.filter(
                workspace_id=workspace_id
            ), 
            pk=pk
        )
    else:
        metric = get_object_or_404(
            GapAnalysisMetric.objects.filter(
                user=request.user,
                workspace__isnull=True,
            ),
            pk=pk
        )

    prepare_gap_metrics_for_display([metric], workspace_id, request.user)

    return render(request, 'dashboard/partials/_gap_analysis_row.html', {'gap': metric})

def refresh_gap_analysis_table(request):
    workspace_id = request.GET.get('workspace') or request.POST.get('workspace') or request.session.get('current_workspace_id')
    if workspace_id:
        gap_analysis = GapAnalysisMetric.objects.filter(
            workspace_id=workspace_id
        ).order_by('category', 'priority')
    else:
        gap_analysis = GapAnalysisMetric.objects.filter(
            user=request.user,
            workspace__isnull=True,
        ).order_by('category', 'priority')

    gap_analysis = prepare_gap_metrics_for_display(gap_analysis, workspace_id, request.user)

    return render(request, 'dashboard/partials/_gap_analysis_table_rows.html', {'gap_analysis': gap_analysis})


# ================================================================
# ACTION ITEM VIEWS
# ================================================================

