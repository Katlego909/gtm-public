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
from gtm.decorators import workspace_permission_required, workspace_admin_required, workspace_member_required
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


from ..parsers import (
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

    client = None
    try:
        from gtm.ai_services import _get_client
        client = _get_client()
    except Exception as exc:
        logger.warning('Vertex AI client unavailable: %s', exc)

    if not client:
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
        ai_response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
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



def _gap_percent_value(metric):
    """Numeric gap percentage used for sorting and report analytics."""
    current = float(metric.current or 0)
    target = float(metric.target or 0)
    if target <= 0:
        return 0.0
    if metric.metric == 'CAC Payback Period':
        return (target - current) / target * 100
    return (current - target) / target * 100


