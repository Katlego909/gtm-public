from .analytics import get_dashboard_context
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
from gtm.decorators import workspace_permission_required, workspace_admin_required, workspace_member_required
from dashboard.models import Channel, ChannelAnalytics, GapAnalysisMetric, GapAnalysisSuggestion, Resource
from .forms import GapAnalysisMetricForm, ActionItemForm, UserProfileForm, ResourceForm
from gtm.views import _compute_scores, _band_for_score
from .utils import calculate_gap_metric_display_properties

logger = logging.getLogger(__name__)

from .document_processors import (
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
# RESOURCE LIBRARY VIEWS
# ================================================================


def _md(text: str) -> str:
    """Convert markdown text to HTML, pre-processing to ensure lists render correctly."""
    if not text:
        return ''
    # Insert blank line before any line that starts a list item (* - + or numbered)
    # if the preceding line is not already blank. Python-markdown requires a blank
    # line before a list block to detect it properly.
    text = re.sub(r'(?m)(?<=\S)\n([ \t]*(?:[*\-+]|\d+\.) )', r'\n\n\1', text)
    # Also ensure blank line after a heading line (### ...) before the next content
    text = re.sub(r'(?m)(^#{1,6} .+)\n(?!\n)', r'\1\n\n', text)
    return markdown.markdown(text, extensions=['extra', 'nl2br', 'sane_lists'])


from .parsers import (
    log_workspace_activity,
    _extract_create_task_command,
    _normalize_task_status,
    _extract_move_task_command,
    _extract_delete_task_command,
    _extract_comment_task_command,
    _resolve_assignee,
    _task_command_queryset,
    _resolve_task_for_command,
    _run_dashboard_action_command,
)



def _resolve_dashboard_workspace(request):
    """Resolve the active workspace from request/query/session for dashboard-scoped UI."""
    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    current_workspace = None
    user_workspaces = []

    if request.user.is_authenticated:
        memberships = WorkspaceMembership.objects.filter(user=request.user, is_active=True).select_related('workspace')
        user_workspaces = [m.workspace for m in memberships]
        if workspace_id:
            try:
                current_workspace = next(w for w in user_workspaces if str(w.id) == str(workspace_id))
            except StopIteration:
                current_workspace = None
        if not current_workspace and user_workspaces:
            current_workspace = user_workspaces[0]
        if current_workspace:
            request.session['current_workspace_id'] = str(current_workspace.id)

    return current_workspace, user_workspaces


def _build_sidebar_notifications_context(request, current_workspace):
    """Build right-sidebar notifications context for all dashboard pages."""
    if current_workspace:
        pending_items = ActionItem.objects.filter(workspace=current_workspace).exclude(status='done').count()
        recent_activity = WorkspaceActivityEvent.objects.filter(
            workspace=current_workspace
        ).filter(
            models.Q(session__isnull=False) | models.Q(object_type='resource')
        ).select_related('actor')[:12]
    else:
        pending_items = ActionItem.objects.filter(
            session__user=request.user,
            workspace__isnull=True,
        ).exclude(status='done').count() if request.user.is_authenticated else 0
        recent_activity = []

    return {
        'pending_items': pending_items,
        'sidebar_recent_activity': recent_activity,
        'sidebar_workspace': current_workspace,
    }


def _normalize_action_text(value):
    text = (value or '').strip().lower()
    text = re.sub(r'[^a-z0-9\s]+', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text


def _build_recent_agent_actions(chat_qs, max_items=5):
    """Build a compact, deduplicated list of chat-based action-oriented events."""
    action_intents = {'dashboard_action', 'execution_plan', 'review_action_items'}
    rows = list(chat_qs.filter(intent__in=action_intents).order_by('-created_at')[:40])

    grouped = []
    by_key = {}
    for chat in rows:
        normalized = _normalize_action_text(chat.message)
        key = (chat.intent or '', normalized)
        if key in by_key:
            by_key[key]['count'] += 1
            continue

        action_type = 'Action'
        if chat.intent == 'dashboard_action':
            action_type = 'Task Action'
        elif chat.intent == 'execution_plan':
            action_type = 'Plan Generated'
        elif chat.intent == 'review_action_items':
            action_type = 'Backlog Review'

        item = {
            'title': chat.message,
            'summary': chat.response,
            'created_at': chat.created_at,
            'count': 1,
            'action_type': action_type,
            'intent': chat.intent,
        }
        by_key[key] = item
        grouped.append(item)

    return grouped[:max_items]


def _collect_recent_action_feed(request, current_workspace, max_items=6):
    """Merge workspace activity and agent action chats into one recent-action feed."""
    if current_workspace:
        chat_qs = ChatMessage.objects.filter(session__workspace=current_workspace)
        workspace_events = list(
            WorkspaceActivityEvent.objects.filter(workspace=current_workspace)
            .filter(models.Q(session__isnull=False) | models.Q(object_type='resource'))
            .select_related('actor')
            .order_by('-created_at')[:30]
        )
    else:
        chat_qs = ChatMessage.objects.filter(session__user=request.user)
        workspace_events = []

    chat_actions = _build_recent_agent_actions(chat_qs, max_items=12)

    unified = []
    for event in workspace_events:
        unified.append({
            'title': event.summary,
            'summary': event.summary,
            'created_at': event.created_at,
            'count': 1,
            'action_type': 'Workspace Activity',
            'intent': event.event_type,
            'source': 'workspace',
        })

    for action in chat_actions:
        unified.append({
            'title': action['title'],
            'summary': action['summary'],
            'created_at': action['created_at'],
            'count': action['count'],
            'action_type': action['action_type'],
            'intent': action['intent'],
            'source': 'agent',
        })

    unified.sort(key=lambda item: item['created_at'], reverse=True)

    deduped = []
    seen = set()
    for item in unified:
        dedupe_key = (_normalize_action_text(item['title']), item['source'])
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        deduped.append(item)
        if len(deduped) >= max_items:
            break

    return deduped


def _get_gap_scope(request):
    """Resolve scope for gap-analysis data and actions."""
    workspace_id = request.GET.get('workspace') or request.POST.get('workspace') or request.session.get('current_workspace_id')
    current_workspace = None

    if workspace_id:
        current_workspace = WorkspaceMembership.objects.filter(
            user=request.user,
            workspace_id=workspace_id,
            is_active=True,
        ).select_related('workspace').first()
        current_workspace = current_workspace.workspace if current_workspace else None

    latest_completed_session = None
    if current_workspace:
        latest_completed_session = AssessmentSession.objects.filter(
            workspace=current_workspace,
            is_completed=True,
        ).order_by('-created_at').first()
    else:
        latest_completed_session = AssessmentSession.objects.filter(
            user=request.user,
            is_completed=True,
        ).order_by('-created_at').first()

    return current_workspace, latest_completed_session


def _fallback_gap_suggestions(cat_scores):
    """Deterministic fallback suggestions when AI is unavailable."""
    if not cat_scores:
        return []

    scores = {c['category'].name: float(c['avg']) for c in cat_scores}

    def clamp(value, lo, hi):
        return max(lo, min(hi, value))

    mapped = [
        {
            'category': 'Lead Generation',
            'metric': 'Monthly Qualified Leads',
            'score': scores.get('Demand', 2.5),
            'base': 40,
            'scale': 22,
            'target_step': 18,
            'priority_if_below': 2.8,
            'recommendation': 'Tighten ICP filters, run weekly campaign reviews, and improve top-of-funnel messaging consistency.',
            'rationale': 'Demand score indicates lead quality/volume opportunity.',
            'higher_is_better': True,
        },
        {
            'category': 'Sales Efficiency',
            'metric': 'Win Rate',
            'score': scores.get('Conversion', 2.5),
            'base': 14,
            'scale': 7,
            'target_step': 8,
            'priority_if_below': 3.0,
            'recommendation': 'Standardize qualification, tighten discovery scripts, and run deal review coaching sessions.',
            'rationale': 'Conversion performance signals pipeline quality and sales process efficiency.',
            'higher_is_better': True,
        },
        {
            'category': 'Customer Success',
            'metric': 'Net Revenue Retention',
            'score': scores.get('Delivery', 2.5),
            'base': 75,
            'scale': 8,
            'target_step': 6,
            'priority_if_below': 3.2,
            'recommendation': 'Strengthen onboarding milestones, proactive success reviews, and expansion signal tracking.',
            'rationale': 'Delivery maturity drives retention and expansion reliability.',
            'higher_is_better': True,
        },
        {
            'category': 'Marketing ROI',
            'metric': 'CAC Payback Period',
            'score': scores.get('Demand', 2.5),
            'base': 18,
            'scale': -2.2,
            'target_step': -2.0,
            'priority_if_below': 2.9,
            'recommendation': 'Reduce low-performing spend, improve conversion quality, and align campaign budgets to top channels.',
            'rationale': 'Acquisition efficiency can improve by optimizing spend-to-revenue cycle time.',
            'higher_is_better': False,
        },
    ]

    suggestions = []
    for item in mapped:
        current = item['base'] + (item['score'] * item['scale'])
        target = current + item['target_step']

        if item['higher_is_better']:
            current = round(clamp(current, 1, 400), 1)
            target = round(clamp(max(target, current + 3), current + 3, 500), 1)
        else:
            current = round(clamp(current, 3, 36), 1)
            target = round(clamp(min(target, current - 0.5), 1, current - 0.5), 1)

        priority = 'High' if item['score'] < item['priority_if_below'] else 'Medium'
        confidence = 78 if priority == 'High' else 70
        suggestions.append({
            'category': item['category'],
            'metric': item['metric'],
            'current': current,
            'target': target,
            'priority': priority,
            'recommendation': item['recommendation'],
            'rationale': item['rationale'],
            'confidence': confidence,
        })

    return suggestions


def _clean_json_payload(raw_text):
    text = (raw_text or '').strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    return text.strip()


def _ai_gap_suggestions_from_assessment(session):
    """Generate structured gap metric suggestions from an assessment session."""
    cat_scores, overall = _compute_scores(session)
    category_payload = [
        {'category': c['category'].name, 'score': round(float(c['avg']), 2)}
        for c in cat_scores
    ]
    weak_questions = list(
        session.responses.filter(score__lte=2).select_related('question__category')[:8]
    )
    weak_payload = [
        {
            'question': r.question.text,
            'category': r.question.category.name,
            'score': r.score,
        }
        for r in weak_questions
    ]

    allowed_categories = [name for name, _ in GapAnalysisMetric.CATEGORY_CHOICES]
    allowed_metrics = list(GapAnalysisMetric.METRIC_FIELD_MAPPING.keys())
    allowed_priorities = [name for name, _ in GapAnalysisMetric.PRIORITY_CHOICES]

    model = None
    try:
        import google.generativeai as genai
        api_key = getattr(settings, 'GEMINI_API_KEY', None)
        if api_key:
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-2.5-flash')
    except Exception as exc:
        logger.warning('AI gap suggestion model unavailable: %s', exc)

    if not model:
        return _fallback_gap_suggestions(cat_scores), {
            'generator': 'fallback',
            'overall_score': round(float(overall), 1),
        }

    prompt = f"""
You are a GTM analyst. Generate 3-5 actionable gap metric suggestions from this assessment.

Rules:
- Output STRICT JSON only (no markdown, no comments).
- Output shape: {{"suggestions": [{{"category":..., "metric":..., "current":..., "target":..., "priority":..., "recommendation":..., "rationale":..., "confidence":...}}]}}
- category must be one of: {allowed_categories}
- metric must be one of: {allowed_metrics}
- priority must be one of: {allowed_priorities}
- confidence must be integer between 50 and 95
- Keep recommendation under 180 chars.

Assessment context:
- Overall score: {round(float(overall), 1)}
- Category scores: {json.dumps(category_payload)}
- Weak answers: {json.dumps(weak_payload)}
""".strip()

    try:
        ai_response = model.generate_content(prompt)
        payload = json.loads(_clean_json_payload(getattr(ai_response, 'text', '')))
        raw_suggestions = payload.get('suggestions', []) if isinstance(payload, dict) else []
    except Exception as exc:
        logger.warning('AI gap suggestion generation failed; using fallback: %s', exc)
        return _fallback_gap_suggestions(cat_scores), {
            'generator': 'fallback_after_ai_error',
            'overall_score': round(float(overall), 1),
        }

    validated = []
    seen_metrics = set()
    for item in raw_suggestions:
        if not isinstance(item, dict):
            continue
        category = item.get('category')
        metric = item.get('metric')
        priority = item.get('priority')
        if category not in allowed_categories or metric not in allowed_metrics or priority not in allowed_priorities:
            continue
        if metric in seen_metrics:
            continue
        try:
            current = float(item.get('current', 0))
            target = float(item.get('target', 0))
            confidence = int(item.get('confidence', 70))
        except (TypeError, ValueError):
            continue

        current = round(max(current, 0), 2)
        target = round(max(target, 0), 2)
        confidence = max(50, min(95, confidence))

        if metric == 'CAC Payback Period' and target >= current:
            target = round(max(1.0, current - 1.0), 2)
        elif metric != 'CAC Payback Period' and target <= current:
            target = round(current + max(1.0, current * 0.1), 2)

        recommendation = (item.get('recommendation') or '').strip()[:180]
        rationale = (item.get('rationale') or '').strip()[:240]
        if not recommendation:
            continue

        seen_metrics.add(metric)
        validated.append({
            'category': category,
            'metric': metric,
            'current': current,
            'target': target,
            'priority': priority,
            'recommendation': recommendation,
            'rationale': rationale,
            'confidence': confidence,
        })

    if not validated:
        validated = _fallback_gap_suggestions(cat_scores)
        generator = 'fallback_after_validation'
    else:
        generator = 'gemini'

    return validated[:5], {
        'generator': generator,
        'overall_score': round(float(overall), 1),
        'category_scores': category_payload,
    }


def _load_pending_gap_suggestions(request, current_workspace):
    if current_workspace:
        qs = GapAnalysisSuggestion.objects.filter(
            workspace=current_workspace,
            status='pending',
            user=request.user,
        )
    else:
        qs = GapAnalysisSuggestion.objects.filter(
            workspace__isnull=True,
            user=request.user,
            status='pending',
        )

    suggestions = list(qs.order_by('-created_at'))
    for suggestion in suggestions:
        pseudo_metric = type('PseudoMetric', (), {
            'metric': suggestion.metric,
            'current': suggestion.current,
            'target': suggestion.target,
            'priority': suggestion.priority,
        })()
        calculate_gap_metric_display_properties(pseudo_metric)
        suggestion.gap_percent = pseudo_metric.gap_percent
        suggestion.gap_class = pseudo_metric.gap_class
    return suggestions


def _gap_metric_scope_queryset(user, workspace, metric_name):
    """Return scope-aware queryset for a metric, used to enforce idempotent writes."""
    if workspace:
        return GapAnalysisMetric.objects.filter(workspace=workspace, metric=metric_name)
    return GapAnalysisMetric.objects.filter(workspace__isnull=True, user=user, metric=metric_name)


def _upsert_gap_metric_in_scope(*, user, workspace, session, payload, source='AI'):
    """Upsert one metric per scope+metric and remove stale duplicates if present."""
    metric_name = payload['metric']
    scope_qs = _gap_metric_scope_queryset(user, workspace, metric_name).order_by('-id')
    existing = scope_qs.first()

    if existing:
        for stale in scope_qs[1:]:
            stale.delete()

        existing.category = payload['category']
        existing.current = payload['current']
        existing.target = payload['target']
        existing.priority = payload['priority']
        existing.recommendation = payload['recommendation']
        existing.source = source
        existing.user = user
        if session:
            existing.session = session
        existing.save()
        return existing, False

    created = GapAnalysisMetric.objects.create(
        category=payload['category'],
        metric=metric_name,
        current=payload['current'],
        target=payload['target'],
        priority=payload['priority'],
        recommendation=payload['recommendation'],
        source=source,
        session=session,
        workspace=workspace,
        user=user,
    )
    return created, True


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


@require_http_methods(["GET"])
@login_required
def refresh_recent_agent_actions(request):
    """Render live recent action feed panel content."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    recent_agent_actions = _collect_recent_action_feed(request, current_workspace, max_items=6)
    return render(request, 'dashboard/partials/recent_agent_actions_panel.html', {
        'recent_agent_actions': recent_agent_actions,
        'current_workspace': current_workspace,
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


@require_http_methods(["GET"])
@login_required
def notifications_panel(request):
    """Render the right sidebar notifications/activity panel."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    context = _build_sidebar_notifications_context(request, current_workspace)
    return render(request, 'dashboard/partials/notifications_panel.html', context)

@workspace_member_required('session')
def refresh_resources(request):
    """Returns the updated resources list - workspace-aware."""
    workspace_id = request.session.get('current_workspace_id')
    if not workspace_id:
        return HttpResponse("Workspace context missing", status=400)
        
    resources = Resource.objects.filter(workspace_id=workspace_id).order_by('category', '-created_at')
    return render(request, 'dashboard/partials/resource_list.html', {'resources': resources})


@workspace_member_required('session')
@vary_on_headers('HX-Request')
def add_edit_resource(request, pk=None):
    workspace_id = request.session.get('current_workspace_id')
    current_workspace = get_object_or_404(Workspace, id=workspace_id)
    
    if pk:
        instance = get_object_or_404(Resource, pk=pk, workspace=current_workspace)
        title = "Edit Resource"
        action_label = "updated"
    else:
        instance = None
        title = "Add Resource"
        action_label = "created"

    if request.method == 'POST':
        form = ResourceForm(request.POST, request.FILES, instance=instance)
        if form.is_valid():
            resource = form.save(commit=False)
            resource.workspace = current_workspace
            resource.uploaded_by = request.user
            resource.save()

            event_type = 'resource_updated' if pk else 'resource_created'
            action_word = 'updated' if pk else 'added'
            log_workspace_activity(
                current_workspace,
                request.user,
                event_type,
                f"{request.user.get_full_name() or request.user.username} {action_word} resource '{resource.name}'.",
                object_type='resource',
                object_id=resource.id,
            )
            
            if request.htmx:
                response = refresh_resources(request)
                response['HX-Trigger'] = json.dumps({
                    'closeModal': True,
                    'workspaceUpdated': True,
                    'resourceToast': {
                        'message': f"Resource {action_label} successfully.",
                        'level': 'success'
                    }
                })
                return response
            return redirect('dashboard')
    else:
        form = ResourceForm(instance=instance)

    context = {
        'form': form,
        'title': title,
        'instance': instance,
        'current_workspace': current_workspace
    }
    
    return render(request, 'dashboard/partials/resource_form.html', context)


@workspace_member_required('session')
@vary_on_headers('HX-Request')
def delete_resource(request, pk):
    workspace_id = request.session.get('current_workspace_id')
    resource = get_object_or_404(Resource, pk=pk, workspace_id=workspace_id)
    
    if request.method == 'POST':
        resource_name = resource.name
        resource_id = resource.id
        resource.delete()

        log_workspace_activity(
            current_workspace,
            request.user,
            'resource_deleted',
            f"{request.user.get_full_name() or request.user.username} deleted resource '{resource_name}'.",
            object_type='resource',
            object_id=resource_id,
        )

        if request.htmx:
            response = refresh_resources(request)
            response['HX-Trigger'] = json.dumps({
                'closeModal': True,
                'resourceToast': {
                    'message': f"Resource '{resource_name}' deleted.",
                    'level': 'success'
                }
            })
            return response
        return redirect('dashboard')
    
    return render(request, 'dashboard/partials/resource_confirm_delete.html', {'instance': resource})

# ================================================================
# GAP ANALYSIS METRIC CRUD VIEWS
# ================================================================

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
                for metric in gap_analysis:
                    calculate_gap_metric_display_properties(metric)
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

@csrf_exempt
def insight_export(request, pk):
    """Export AI insight/playbook as a text file."""
    # Only allow access to insights in user's workspace
    workspace_id = request.session.get('current_workspace_id')
    if workspace_id:
        insight = get_object_or_404(ResultSnapshot, pk=pk, session__workspace_id=workspace_id)
    else:
        insight = get_object_or_404(ResultSnapshot, pk=pk, session__user=request.user)
    content = insight.ai_playbook or ''
    response = HttpResponse(content, content_type='text/plain')
    response['Content-Disposition'] = f'attachment; filename=insight_{pk}.txt'
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
def workspace_hub(request):
    """Dedicated workspace management hub for team and resources."""
    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    current_workspace = None
    user_workspaces = []
    
    if request.user.is_authenticated:
        memberships = WorkspaceMembership.objects.filter(user=request.user).select_related('workspace')
        user_workspaces = [m.workspace for m in memberships]
        if workspace_id:
            try:
                current_workspace = next(w for w in user_workspaces if str(w.id) == workspace_id)
            except StopIteration:
                current_workspace = None
        if not current_workspace and user_workspaces:
            current_workspace = user_workspaces[0]

        if current_workspace:
            request.session['current_workspace_id'] = str(current_workspace.id)

    # Resource Library
    resources = []
    team_members = []
    workspace_memberships = []
    pending_invites = []
    accepted_awaiting = []

    if current_workspace:
        resources = Resource.objects.filter(workspace=current_workspace).order_by('category', '-created_at')
        
        # Auto-create memberships for accepted invitations
        accepted_invites = WorkspaceInvitation.objects.filter(workspace=current_workspace).filter(
            models.Q(is_accepted=True) | models.Q(accepted_at__isnull=False)
        )
        for invite in accepted_invites:
            matched_user = User.objects.filter(email__iexact=invite.email).first()
            if matched_user and not WorkspaceMembership.objects.filter(workspace=current_workspace, user=matched_user).exists():
                WorkspaceMembership.objects.create(
                    workspace=current_workspace,
                    user=matched_user,
                    role=invite.role,
                    invited_by=invite.invited_by,
                )

        workspace_memberships = WorkspaceMembership.objects.filter(workspace=current_workspace).select_related('user')
        team_members = [m.user for m in workspace_memberships]
        pending_invites = WorkspaceInvitation.objects.filter(
            workspace=current_workspace, accepted_at__isnull=True, is_accepted=False
        )
        accepted_awaiting = WorkspaceInvitation.objects.filter(workspace=current_workspace).filter(
            models.Q(is_accepted=True) | models.Q(accepted_at__isnull=False)
        ).exclude(email__in=User.objects.values_list('email', flat=True))

    context = {
        'current_workspace': current_workspace,
        'user_workspaces': user_workspaces,
        'resources': resources,
        'team_members': team_members,
        'workspace_memberships': workspace_memberships,
        'pending_invites': pending_invites,
        'accepted_awaiting': accepted_awaiting,
        'page_title': 'Workspace Hub'
    }

    if request.htmx:
        response = render(request, 'dashboard/partials/workspace_hub_content.html', context)
        response['HX-Trigger'] = 'refreshNotifications, refreshAgentActions'
        return response
    return render(request, 'dashboard/workspace_hub.html', context)

@vary_on_headers('HX-Request')
@login_required
def tasks_board(request):
    """Dedicated full-screen tasks/Kanban board view."""
    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    current_workspace = None
    user_workspaces = []
    
    if request.user.is_authenticated:
        memberships = WorkspaceMembership.objects.filter(user=request.user).select_related('workspace')
        user_workspaces = [m.workspace for m in memberships]
        if workspace_id:
            try:
                current_workspace = next(w for w in user_workspaces if str(w.id) == workspace_id)
            except StopIteration:
                current_workspace = None
        if not current_workspace and user_workspaces:
            current_workspace = user_workspaces[0]

        if current_workspace:
            request.session['current_workspace_id'] = str(current_workspace.id)

    # Filter action items
    if current_workspace:
        action_items_qs = ActionItem.objects.filter(workspace=current_workspace)
    else:
        action_items_qs = ActionItem.objects.filter(session__user=request.user, workspace__isnull=True) if request.user.is_authenticated else ActionItem.objects.none()
    
    top_todo = action_items_qs.filter(status='todo').order_by('due_date').select_related('assigned_to')
    top_doing = action_items_qs.filter(status='doing').order_by('due_date').select_related('assigned_to')
    top_done = action_items_qs.filter(status='done').order_by('-created_at').select_related('assigned_to')
    
    # Team members for assignment dropdowns
    team_members = []
    if current_workspace:
        memberships = WorkspaceMembership.objects.filter(workspace=current_workspace).select_related('user')
        team_members = [m.user for m in memberships]

    context = {
        'current_workspace': current_workspace,
        'user_workspaces': user_workspaces,
        'top_todo': top_todo,
        'top_doing': top_doing,
        'top_done': top_done,
        'team_members': team_members,
        'page_title': 'Tasks & Execution'
    }

    if request.htmx:
        response = render(request, 'dashboard/partials/tasks_content.html', context)
        response['HX-Trigger'] = 'refreshNotifications, refreshAgentActions'
        return response
    return render(request, 'dashboard/tasks.html', context)


@vary_on_headers('HX-Request')
@login_required
def agent_hub(request):
    """Dedicated GTM agent page with persistent, session-scoped conversation context."""
    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    agent_session_id = request.GET.get('agent_session')
    current_workspace = None
    user_workspaces = []

    if request.user.is_authenticated:
        memberships = WorkspaceMembership.objects.filter(user=request.user).select_related('workspace')
        user_workspaces = [m.workspace for m in memberships]
        if workspace_id:
            try:
                current_workspace = next(w for w in user_workspaces if str(w.id) == workspace_id)
            except StopIteration:
                current_workspace = None
        if not current_workspace and user_workspaces:
            current_workspace = user_workspaces[0]

        if current_workspace:
            request.session['current_workspace_id'] = str(current_workspace.id)

    if current_workspace:
        assessments_qs = AssessmentSession.objects.filter(workspace=current_workspace)
        pending_items = ActionItem.objects.filter(workspace=current_workspace).exclude(status='done').count()
    else:
        assessments_qs = AssessmentSession.objects.filter(user=request.user)
        pending_items = ActionItem.objects.filter(session__user=request.user).exclude(status='done').count()

    completed_sessions = assessments_qs.filter(is_completed=True).order_by('-created_at')[:10]
    agent_session_options = [
        {
            'uuid': str(session.uuid),
            'label': f"{session.company_name or 'Unnamed'} • {session.created_at.strftime('%b %d, %Y')}",
        }
        for session in completed_sessions
    ]
    if not agent_session_options:
        agent_session_options = [
            {
                'uuid': str(session.uuid),
                'label': f"{session.company_name or 'Unnamed'} • {session.created_at.strftime('%b %d, %Y')}",
            }
            for session in assessments_qs.order_by('-created_at')[:10]
        ]

    dashboard_agent_session = None
    if agent_session_options:
        if agent_session_id and any(option['uuid'] == agent_session_id for option in agent_session_options):
            dashboard_agent_session = assessments_qs.filter(uuid=agent_session_id).first()
        if not dashboard_agent_session:
            dashboard_agent_session = assessments_qs.filter(uuid=agent_session_options[0]['uuid']).first()

    dashboard_agent_prompts = get_suggested_prompts(dashboard_agent_session) if dashboard_agent_session else []
    dashboard_agent_history = []
    if dashboard_agent_session:
        history_qs = ChatMessage.objects.filter(session=dashboard_agent_session).order_by('-created_at')[:50]
        chats = list(reversed(history_qs))
        for chat in chats:
            chat.response_html = _md(chat.response or '')
        dashboard_agent_history = chats

    context = {
        'current_workspace': current_workspace,
        'user_workspaces': user_workspaces,
        'agent_session_options': agent_session_options,
        'dashboard_agent_session': dashboard_agent_session,
        'dashboard_agent_prompts': dashboard_agent_prompts,
        'dashboard_agent_history': dashboard_agent_history,
        'pending_items': pending_items,
        'page_title': 'Agent',
    }

    if request.htmx:
        response = render(request, 'dashboard/partials/agent_content.html', context)
        response['HX-Trigger'] = 'refreshNotifications, refreshAgentActions'
        return response
    return render(request, 'dashboard/agent.html', context)

@vary_on_headers('HX-Request')
@login_required
def dashboard(request):
    """Main dashboard view - now workspace-aware for team collaboration."""
    
    # Get workspace context
    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    agent_session_id = request.GET.get('agent_session')
    current_workspace = None
    user_workspaces = []
    
    if request.user.is_authenticated:
        # Get user's workspaces
        memberships = WorkspaceMembership.objects.filter(user=request.user).select_related('workspace')
        user_workspaces = [m.workspace for m in memberships]
        
        # If workspace_id provided, validate user has access
        if workspace_id:
            try:
                current_workspace = next(w for w in user_workspaces if str(w.id) == workspace_id)
            except StopIteration:
                current_workspace = None
        
        # Default to first workspace if none selected
        if not current_workspace and user_workspaces:
            current_workspace = user_workspaces[0]

        if current_workspace:
            request.session['current_workspace_id'] = str(current_workspace.id)
        
        # Auto-fix orphaned assessments: associate user's assessments without workspace
        if current_workspace:
            orphaned_assessments = AssessmentSession.objects.filter(
                user=request.user,
                workspace__isnull=True
            )
            if orphaned_assessments.exists():
                orphaned_assessments.update(workspace=current_workspace)
            
            # Auto-fix orphaned action items (from user's sessions or assigned to user)
            orphaned_items = ActionItem.objects.filter(
                workspace__isnull=True
            ).filter(
                models.Q(session__user=request.user) | models.Q(assigned_to=request.user)
            )
            if orphaned_items.exists():
                orphaned_items.update(workspace=current_workspace)
    
    context = get_dashboard_context(request, current_workspace, user_workspaces, agent_session_id)

    if request.htmx:
        response = render(request, 'dashboard/partials/dashboard_content.html', context)
        response['HX-Trigger'] = 'refreshNotifications, refreshAgentActions'
        return response

    return render(request, 'dashboard/home.html', context)


@require_http_methods(["POST"])
@login_required
def dashboard_agent_api(request):
    """Run the GTM agent inline from the dashboard without navigating to chat."""
    content_type = (request.content_type or '').lower()
    uploaded_files = []
    if 'multipart/form-data' in content_type:
        session_id = str(request.POST.get('session_id', '')).strip()
        message = (request.POST.get('message') or '').strip()
        file_type = (request.POST.get('file_type') or 'other').strip()
        uploaded_files = request.FILES.getlist('attachments')
    else:
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

        session_id = str(data.get('session_id', '')).strip()
        message = (data.get('message') or '').strip()
        file_type = (data.get('file_type') or 'other').strip()

    if not session_id:
        return JsonResponse({"success": False, "error": "Select an assessment first."}, status=400)
    if not message:
        return JsonResponse({"success": False, "error": "Message cannot be empty."}, status=400)

    try:
        session = AssessmentSession.objects.get(pk=session_id)
    except AssessmentSession.DoesNotExist:
        return JsonResponse({"success": False, "error": "Assessment session not found."}, status=404)

    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    current_workspace = None
    if workspace_id:
        if str(session.workspace_id) != str(workspace_id):
            return JsonResponse({"success": False, "error": "That assessment is not in the current workspace."}, status=403)
        current_workspace = Workspace.objects.filter(id=workspace_id).first()
    elif session.user_id != request.user.id:
        return JsonResponse({"success": False, "error": "You do not have access to that assessment."}, status=403)

    attachments, attachment_context, attachment_warnings = _process_agent_attachments(uploaded_files)

    # 🔹 [VISION BRIDGE] Save strategic evidence files to GTMFile
    for uploaded_file in uploaded_files:
        suffix = Path(uploaded_file.name or '').suffix.lower()
        # If it's an image or PDF, it's potential strategic evidence
        if suffix in AGENT_IMAGE_EXTENSIONS or suffix == '.pdf':
            try:
                # Create the GTMFile record
                gtm_file = GTMFile.objects.create(
                    session=session,
                    file=uploaded_file,
                    file_type=file_type
                )
                
                # Trigger asynchronous audit in a background thread
                from gtm.ai_auditor import audit_strategic_evidence_async
                import threading
                threading.Thread(
                    target=audit_strategic_evidence_async,
                    args=(gtm_file.id,),
                    daemon=True
                ).start()
                
                logger.info(f"Vision Bridge: Saved {uploaded_file.name} as GTMFile {gtm_file.id}")
            except Exception as e:
                logger.error(f"Vision Bridge: Failed to save {uploaded_file.name}: {e}")

    action_result = _run_dashboard_action_command(request, session, current_workspace, message)
    if action_result:
        ChatMessage.objects.create(
            session=session,
            user=request.user,
            message=message,
            response=action_result.get("response", ""),
            attachments=attachments,
            intent=action_result.get("intent", "dashboard_action"),
        )
        action_result["uploaded_attachments"] = attachments
        if attachment_warnings:
            action_result["attachment_warnings"] = attachment_warnings
        return JsonResponse(action_result, status=200)

    result = process_chat_message(
        session_id=str(session.uuid),
        message=message,
        user=request.user,
        supplemental_context=attachment_context,
    )

    if result.get("success"):
        ChatMessage.objects.create(
            session=session,
            user=request.user,
            message=message,
            response=result.get("response", ""),
            attachments=attachments,
            intent=result.get("intent", ""),
        )
        result["response_html"] = _md(result.get("response", ""))
        result["uploaded_attachments"] = attachments
        if attachment_warnings:
            result["attachment_warnings"] = attachment_warnings

    return JsonResponse(result, status=200 if result.get("success") else 500)


@require_http_methods(["POST"])
@login_required
def dashboard_agent_clear_api(request):
    """Clear dashboard agent chat history for a selected assessment session."""
    content_type = (request.content_type or '').lower()
    if 'multipart/form-data' in content_type:
        session_id = str(request.POST.get('session_id', '')).strip()
    else:
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)
        session_id = str(data.get('session_id', '')).strip()

    if not session_id:
        return JsonResponse({"success": False, "error": "Select an assessment first."}, status=400)

    try:
        session = AssessmentSession.objects.get(pk=session_id)
    except AssessmentSession.DoesNotExist:
        return JsonResponse({"success": False, "error": "Assessment session not found."}, status=404)

    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    if workspace_id:
        if str(session.workspace_id) != str(workspace_id):
            return JsonResponse({"success": False, "error": "That assessment is not in the current workspace."}, status=403)
    elif session.user_id != request.user.id:
        return JsonResponse({"success": False, "error": "You do not have access to that assessment."}, status=403)

    # 🔹 [DEEP CLEAR] Wipe chat messages AND strategic evidence files
    deleted_count, _ = ChatMessage.objects.filter(session=session).delete()
    session.evidence_files.all().delete()
    return JsonResponse({
        "success": True,
        "deleted_count": deleted_count,
        "message": "Chat history cleared.",
    })


@require_http_methods(["GET"])
@login_required
def dashboard_agent_context_api(request):
    """Return session options and prompts for the global dashboard agent widget."""
    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    requested_session_id = (request.GET.get('session_id') or '').strip()

    assessments_qs = AssessmentSession.objects.none()
    if workspace_id:
        has_membership = WorkspaceMembership.objects.filter(
            user=request.user,
            workspace_id=workspace_id,
            is_active=True,
        ).exists()
        if not has_membership:
            return JsonResponse({"success": False, "error": "Access denied to this workspace."}, status=403)
        assessments_qs = AssessmentSession.objects.filter(workspace_id=workspace_id)
    else:
        assessments_qs = AssessmentSession.objects.filter(user=request.user)

    completed_sessions = list(assessments_qs.filter(is_completed=True).order_by('-created_at')[:20])
    session_pool = completed_sessions if completed_sessions else list(assessments_qs.order_by('-created_at')[:20])

    options = [
        {
            'uuid': str(session.uuid),
            'label': f"{session.company_name or 'Unnamed'} • {session.created_at.strftime('%b %d, %Y')}",
        }
        for session in session_pool
    ]

    session_by_id = {str(session.uuid): session for session in session_pool}
    session_memory_key = f"dashboard_agent_session_id:{workspace_id or 'personal'}"
    remembered_session_id = request.session.get(session_memory_key)

    selected_session = None
    if requested_session_id and requested_session_id in session_by_id:
        selected_session = session_by_id[requested_session_id]
    elif remembered_session_id and remembered_session_id in session_by_id:
        selected_session = session_by_id[remembered_session_id]
    elif session_pool:
        selected_session = session_pool[0]

    selected_id = str(selected_session.uuid) if selected_session else None
    if selected_id:
        request.session[session_memory_key] = selected_id

    prompts = get_suggested_prompts(selected_session) if selected_session else []

    history = []
    if selected_session:
        recent_chats = ChatMessage.objects.filter(session=selected_session).order_by('-created_at')[:40]
        for chat in reversed(list(recent_chats)):
            history.append({
                'message': chat.message,
                'response': chat.response,
                'response_html': _md(chat.response or ''),
                'attachments': chat.attachments or [],
                'intent': chat.intent,
                'created_at': chat.created_at.isoformat(),
            })

    return JsonResponse({
        "success": True,
        "options": options,
        "selected_session_id": selected_id,
        "prompts": prompts,
        "history": history,
    })

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
    for gap in gap_analysis:
        calculate_gap_metric_display_properties(gap)
            
    return render(request, 'dashboard/partials/gap_analysis_table.html', {'gap_analysis': gap_analysis})


def _gap_percent_value(metric):
    """Numeric gap percentage used for sorting and report analytics."""
    current = float(metric.current or 0)
    target = float(metric.target or 0)
    if target <= 0:
        return 0.0
    if metric.metric == 'CAC Payback Period':
        return (target - current) / target * 100
    return (current - target) / target * 100


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

    metrics = list(metrics_qs)
    for metric in metrics:
        calculate_gap_metric_display_properties(metric)
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
    
    calculate_gap_metric_display_properties(metric)
        
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
    
    for metric in gap_analysis:
        calculate_gap_metric_display_properties(metric)
            
    return render(request, 'dashboard/partials/_gap_analysis_table_rows.html', {'gap_analysis': gap_analysis})


# ================================================================
# ACTION ITEM VIEWS
# ================================================================

def refresh_action_items(request):
    """Returns the updated action items board - workspace-aware."""
    
    # Get workspace context from request or session
    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    current_workspace = None
    
    if request.user.is_authenticated and workspace_id:
        memberships = WorkspaceMembership.objects.filter(user=request.user).select_related('workspace')
        user_workspaces = [m.workspace for m in memberships]
        try:
            current_workspace = next(w for w in user_workspaces if str(w.id) == str(workspace_id))
        except StopIteration:
            current_workspace = None
    
    # Filter action items by workspace
    if current_workspace:
        action_items_qs = ActionItem.objects.filter(workspace=current_workspace)
    else:
        action_items_qs = ActionItem.objects.filter(session__user=request.user, workspace__isnull=True) if request.user.is_authenticated else ActionItem.objects.none()

    action_items_qs = action_items_qs.prefetch_related('comments__user')
    
    top_todo = action_items_qs.filter(status='todo').order_by('due_date').select_related('assigned_to')
    top_doing = action_items_qs.filter(status='doing').order_by('due_date').select_related('assigned_to')
    top_done = action_items_qs.filter(status='done').order_by('-created_at').select_related('assigned_to')
    
    # Get team members for assignments
    team_members = []
    if current_workspace:
        memberships = WorkspaceMembership.objects.filter(workspace=current_workspace).select_related('user')
        team_members = [m.user for m in memberships]
    
    context = {
        'top_todo': list(top_todo),
        'top_doing': list(top_doing),
        'top_done': list(top_done),
        'current_workspace': current_workspace,
        'team_members': team_members,
    }
    return render(request, 'dashboard/partials/action_items.html', context)


@workspace_member_required('session')
@vary_on_headers('HX-Request')
def add_edit_action_item(request, pk=None):
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
            instance = get_object_or_404(ActionItem, pk=pk, workspace=current_workspace)
            # Creator or assignee can edit
            is_creator = instance.created_by == request.user
            is_assignee = instance.assigned_to == request.user
            is_unclaimed = instance.created_by is None
            if not (is_creator or is_assignee or is_unclaimed):
                if request.htmx:
                    return render(request, 'dashboard/partials/error_modal.html', {
                        'title': 'Access Denied',
                        'message': 'You can only edit action items that you created or are assigned to.'
                    })
                messages.error(request, 'You can only edit your own action items.')
                return redirect('dashboard')
        else:
            instance = get_object_or_404(
                ActionItem.objects.filter(pk=pk, workspace__isnull=True).filter(
                    models.Q(created_by=request.user) | models.Q(session__user=request.user) | models.Q(created_by__isnull=True)
                )
            )
        title = "Edit Action Item"
    else:
        instance = None
        title = "Add Action Item"

    if request.method == 'POST':
        form = ActionItemForm(request.POST, instance=instance, workspace=current_workspace)
        if form.is_valid():
            instance = form.save(commit=False)
            is_new_item = not bool(instance.pk)
            # Auto-assign workspace and creator for new action items
            if not instance.pk:
                instance.workspace = current_workspace
                instance.created_by = request.user
            instance.save()

            if is_new_item:
                log_workspace_activity(
                    current_workspace,
                    request.user,
                    'task_created',
                    f"{request.user.get_full_name() or request.user.username} created task '{instance.note[:80]}'.",
                    object_type='action_item',
                    object_id=instance.id,
                    session=instance.session,
                )

            # Return the updated action items board
            if request.htmx:
                return refresh_action_items(request)
            return redirect('dashboard')
        else:
            # Return form with errors
            context = {
                'form': form,
                'title': title,
                'instance': instance
            }
            return render(request, 'dashboard/partials/action_item_form.html', context)
    else:
        try:
            form = ActionItemForm(instance=instance, workspace=current_workspace)
        except Exception as e:
            # Fallback form if workspace issues
            form = ActionItemForm(instance=instance)

    context = {
        'form': form,
        'title': title,
        'instance': instance
    }
    
    return render(request, 'dashboard/partials/action_item_form.html', context)


@workspace_member_required('session')
@vary_on_headers('HX-Request')
def delete_action_item(request, pk):
    # Get current workspace context
    workspace_id = request.session.get('current_workspace_id')
    current_workspace = None
    if workspace_id:
        try:
            current_workspace = Workspace.objects.get(id=workspace_id)
        except Workspace.DoesNotExist:
            pass
    
    # First, check if the action item exists in the workspace
    if current_workspace:
        try:
            action_item = ActionItem.objects.get(
                pk=pk, 
                workspace=current_workspace
            )
        except ActionItem.DoesNotExist:
            if request.htmx:
                return render(request, 'dashboard/partials/error_modal.html', {
                    'title': 'Item Not Found',
                    'message': 'This action item does not exist or has been removed.'
                })
            raise Http404
            
        # Only the creator can delete (claim unclaimed items)
        if action_item.created_by is None:
            action_item.created_by = request.user
            action_item.save(update_fields=['created_by'])
        elif action_item.created_by != request.user:
            if request.htmx:
                return render(request, 'dashboard/partials/error_modal.html', {
                    'title': 'Access Denied',
                    'message': 'You can only delete action items that you created.'
                })
            messages.error(request, 'You can only delete action items that you created.')
            return redirect('dashboard')
            
        instance = action_item
    else:
        try:
            instance = ActionItem.objects.get(
                pk=pk, 
                session__user=request.user, 
                workspace__isnull=True
            )
        except ActionItem.DoesNotExist:
            if request.htmx:
                return render(request, 'dashboard/partials/error_modal.html', {
                    'title': 'Item Not Found',
                    'message': 'This action item does not exist or has been removed.'
                })
            raise Http404
    
    if request.method == 'POST':
        deleted_note = instance.note
        deleted_id = instance.id
        deleted_session = instance.session
        instance.delete()

        log_workspace_activity(
            current_workspace,
            request.user,
            'task_deleted',
            f"{request.user.get_full_name() or request.user.username} deleted task '{deleted_note[:80]}'.",
            object_type='action_item',
            object_id=deleted_id,
            session=deleted_session,
        )

        # Return the updated action items board
        if request.htmx:
            return refresh_action_items(request)
        return redirect('dashboard')
    
    # Show delete confirmation modal
    context = {'instance': instance}
    return render(request, 'dashboard/partials/action_item_confirm_delete.html', context)


@login_required
def move_action_item(request, pk, new_status):
    if request.method == 'POST':
        # Get current workspace context
        workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
        current_workspace = None
        if workspace_id:
            try:
                from gtm.models_workspace import Workspace
                current_workspace = Workspace.objects.get(id=workspace_id)
            except Workspace.DoesNotExist:
                pass

        # In workspace mode, enforce membership and task assignment permission.
        if current_workspace:
            membership = WorkspaceMembership.objects.filter(
                user=request.user,
                workspace=current_workspace,
                is_active=True,
            ).first()
            if not membership or not membership.can_assign_tasks:
                return JsonResponse({'error': 'Permission denied'}, status=403)
        
        # Only allow access to action items in user's workspace
        if current_workspace:
            item = get_object_or_404(ActionItem, pk=pk, workspace=current_workspace)
        else:
            item = get_object_or_404(ActionItem, pk=pk, session__user=request.user, workspace__isnull=True)
        
        if new_status in ['todo', 'doing', 'done']:
            if new_status == 'done' and item.status != 'done':
                from django.utils import timezone
                item.completed_at = timezone.now()
            elif new_status != 'done':
                item.completed_at = None
            item.status = new_status
            item.save()
            log_workspace_activity(
                current_workspace,
                request.user,
                'task_moved',
                f"{request.user.get_full_name() or request.user.username} moved task '{item.note[:80]}' to {item.get_status_display()}.",
                object_type='action_item',
                object_id=item.id,
                metadata={'status': new_status},
                session=item.session,
            )

            return refresh_action_items(request)
    return HttpResponse(status=400)


# ================================================================
# PROFILE VIEW
# ================================================================

@vary_on_headers('HX-Request')
def profile(request):
    """User profile management page."""
    if request.method == 'POST':
        form = UserProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            if request.htmx:
                response = HttpResponse(render(request, 'dashboard/partials/profile_content.html', {
                    'form': form,
                    'success': True,
                    'message': 'Profile updated successfully!'
                }).content)
                return response
            return redirect('profile')
    else:
        form = UserProfileForm(instance=request.user)
    
    context = {
        'form': form,
    }
    
    if request.htmx:
        return render(request, 'dashboard/partials/profile_content.html', context)
    return render(request, 'dashboard/profile.html', context)


# ================================================================
# TEAM COLLABORATION VIEWS
# ================================================================

@workspace_permission_required('can_assign_tasks', 'session')
def assign_action_item(request, action_id):
    """Assign an action item to a team member."""
    # Workspace context is automatically added by decorator
    current_workspace = request.workspace
    
    # Get the action item (workspace access already verified)
    if current_workspace:
        action_item = get_object_or_404(ActionItem, id=action_id, workspace=current_workspace)
    else:
        action_item = get_object_or_404(ActionItem, id=action_id, session__user=request.user, workspace__isnull=True)
    # Check user has access to this action item's workspace (already checked by decorator)
    if request.method == 'POST':
        assigned_to_id = request.POST.get('assigned_to')
        if assigned_to_id:
            # Verify the assigned user is in the workspace
            if action_item.workspace:
                assigned_user = get_object_or_404(
                    WorkspaceMembership.objects.filter(workspace=action_item.workspace)
                    .select_related('user'), user_id=assigned_to_id
                ).user
            else:
                assigned_user = get_object_or_404(User, id=assigned_to_id)
            
            action_item.assigned_to = assigned_user
            action_item.save()
            
            if request.htmx:
                # Return updated action item card
                workspace_id = action_item.workspace.id if action_item.workspace else None
                request.GET = request.GET.copy()
                request.GET['workspace'] = workspace_id
                return refresh_action_items(request)
            
            return JsonResponse({'success': True})
        
    return JsonResponse({'error': 'Invalid request'}, status=400)


@workspace_permission_required('can_assign_tasks', 'session')
def unassign_action_item(request, action_id):
    """Remove assignment from an action item."""
    # Workspace context is automatically added by decorator
    current_workspace = request.workspace
    
    # Get the action item (workspace access already verified)
    if current_workspace:
        action_item = get_object_or_404(ActionItem, id=action_id, workspace=current_workspace)
    else:
        action_item = get_object_or_404(ActionItem, id=action_id, session__user=request.user, workspace__isnull=True)
    
    if request.method == 'POST':
        action_item.assigned_to = None
        action_item.save()
        
        if request.htmx:
            workspace_id = action_item.workspace.id if action_item.workspace else None
            request.GET = request.GET.copy()
            request.GET['workspace'] = workspace_id
            return refresh_action_items(request)
        
        return JsonResponse({'success': True})
    
    return JsonResponse({'error': 'Invalid request'}, status=400)


@workspace_member_required('session')
@vary_on_headers('HX-Request')
def add_action_item_comment(request, action_id):
    """Add a comment to an action item in the current workspace."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    note = (request.POST.get('text') or '').strip()
    if not note:
        if request.htmx:
            response = refresh_action_items(request)
            response['HX-Trigger'] = json.dumps({
                'resourceToast': {
                    'message': 'Comment cannot be empty.',
                    'level': 'warning'
                }
            })
            return response
        return JsonResponse({'error': 'Comment cannot be empty.'}, status=400)

    workspace = getattr(request, 'workspace', None)
    if workspace:
        action_item = get_object_or_404(ActionItem, id=action_id, workspace=workspace)
    else:
        action_item = get_object_or_404(ActionItem, id=action_id, session__user=request.user, workspace__isnull=True)

    ActionItemComment.objects.create(
        action_item=action_item,
        user=request.user,
        text=note,
    )

    log_workspace_activity(
        workspace,
        request.user,
        'comment_added',
        f"{request.user.get_full_name() or request.user.username} commented on task '{action_item.note[:80]}'.",
        object_type='action_item',
        object_id=action_item.id,
        session=action_item.session,
    )

    if request.htmx:
        response = refresh_action_items(request)
        response['HX-Trigger'] = json.dumps({
            'resourceToast': {
                'message': 'Comment added.',
                'level': 'success'
            }
        })
        return response
    return JsonResponse({'success': True})


@workspace_member_required('session')
@vary_on_headers('HX-Request')
def delete_action_item_comment(request, comment_id):
    """Delete a comment from an action item (author or workspace admin/consultant)."""
    if request.method != 'POST':
        return JsonResponse({'error': 'Invalid request'}, status=400)

    workspace = getattr(request, 'workspace', None)
    if workspace:
        comment = get_object_or_404(
            ActionItemComment.objects.select_related('action_item', 'user'),
            id=comment_id,
            action_item__workspace=workspace,
        )
    else:
        comment = get_object_or_404(
            ActionItemComment.objects.select_related('action_item', 'user'),
            id=comment_id,
            action_item__session__user=request.user,
            action_item__workspace__isnull=True,
        )

    membership = getattr(request, 'membership', None)
    is_admin = bool(membership and membership.role in ['admin', 'funti3r_consultant'])
    if comment.user != request.user and not is_admin:
        if request.htmx:
            return render(request, 'dashboard/partials/error_modal.html', {
                'title': 'Access Denied',
                'message': 'You can only delete your own comments.'
            })
        return JsonResponse({'error': 'Permission denied'}, status=403)

    comment.delete()

    log_workspace_activity(
        workspace,
        request.user,
        'comment_deleted',
        f"{request.user.get_full_name() or request.user.username} deleted a comment on task '{comment.action_item.note[:80]}'.",
        object_type='action_item',
        object_id=comment.action_item.id,
        session=comment.action_item.session,
    )

    if request.htmx:
        response = refresh_action_items(request)
        response['HX-Trigger'] = json.dumps({
            'resourceToast': {
                'message': 'Comment deleted.',
                'level': 'success'
            }
        })
        return response
    return JsonResponse({'success': True})



@login_required
def create_workspace_dashboard(request):
    """Create workspace from dashboard and return modal partial for HTMX."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            workspace = Workspace.create_for_user(name=name, user=request.user)
            # On success, trigger dashboard refresh or close modal via HTMX
            return render(request, 'dashboard/partials/workspace_create_modal.html', {
                'success': True,
                'workspace': workspace,
            })
        # If error, re-render modal with error message
        return render(request, 'dashboard/partials/workspace_create_modal.html', {
            'error': 'Workspace name is required',
        })
    # On GET, render the modal partial
    return render(request, 'dashboard/partials/workspace_create_modal.html')


@login_required

def invite_to_workspace(request, workspace_id):
    """Handle team invitations from the dashboard via HTMX modal."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    dashboard_url = f'/dashboard/?workspace={workspace_id}'

    # Permission check
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
    if not membership or membership.role not in ['admin', 'manager']:
        return render(request, 'dashboard/partials/error_modal.html', {
            'error': "You don't have permission to invite members."
        })

    error = None
    success = False
    email = ''
    role = 'contributor'
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        role = request.POST.get('role', 'contributor')

        if not email:
            error = 'Email address is required.'
        elif WorkspaceInvitation.objects.filter(workspace=workspace, email__iexact=email, accepted_at__isnull=True).exists():
            error = f'An invitation to {email} is already pending.'
        elif WorkspaceMembership.objects.filter(workspace=workspace, user__email__iexact=email).exists():
            error = f'{email} is already a member of this workspace.'
        else:
            invitation = WorkspaceInvitation.objects.create(
                workspace=workspace,
                email=email,
                role=role,
                invited_by=request.user,
            )
            try:
                from gtm.utils_email import send_workspace_invitation_email
                send_workspace_invitation_email(invitation, request)
                success = True
            except Exception as e:
                invitation.delete()
                error = f'Failed to send invitation email: {e}'

    if success:
        response = render(request, 'dashboard/partials/invite_member_modal.html', {
            'success': True,
            'workspace': workspace,
        })
        # Trigger background refresh of the hub
        response['HX-Trigger'] = json.dumps({
            'workspaceUpdated': True,
            'resourceToast': {'message': f'Invitation sent to {email}', 'level': 'success'}
        })
        return response
    response = render(request, 'dashboard/partials/invite_member_modal.html', {
        'workspace': workspace,
        'error': error,
        'email': email,
        'role': role,
    })
    if error and request.htmx:
        # Show error toast using HX-Trigger
        response['HX-Trigger'] = json.dumps({
            'resourceToast': {'message': error, 'level': 'error'}
        })
    return response
