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


def _parse_agent_request_payload(request, require_session=True):
    """Parse an agent chat POST body (JSON or multipart) shared by the
    session-scoped and workspace-scoped agent chat endpoints.

    Returns (session_id, message, file_type, uploaded_files, error_response).
    error_response is a JsonResponse if validation failed; callers should
    return it directly when it is not None.
    """
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
            return None, None, None, None, JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

        session_id = str(data.get('session_id', '')).strip()
        message = (data.get('message') or '').strip()
        file_type = (data.get('file_type') or 'other').strip()

    if require_session and not session_id:
        return None, None, None, None, JsonResponse({"success": False, "error": "Select an assessment first."}, status=400)
    if not message:
        return None, None, None, None, JsonResponse({"success": False, "error": "Message cannot be empty."}, status=400)

    return session_id, message, file_type, uploaded_files, None



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

    ai_credits_remaining = ai_credits_budget = ai_credits_reset_at = ai_credits_used = None
    ai_credits_percent_used = 0
    ai_credits_low = False
    if current_workspace:
        from gtm.ai_credits import can_spend
        credit_check = can_spend(workspace=current_workspace)
        if credit_check.account is not None:
            ai_credits_remaining = credit_check.remaining
            ai_credits_budget = credit_check.account.token_budget
            ai_credits_reset_at = credit_check.reset_at
            ai_credits_used = max(ai_credits_budget - ai_credits_remaining, 0)
            ai_credits_low = ai_credits_budget > 0 and (ai_credits_remaining / ai_credits_budget) < 0.1
            if ai_credits_budget > 0:
                ai_credits_percent_used = min(round(ai_credits_used / ai_credits_budget * 100), 100)

    return {
        'pending_items': pending_items,
        'sidebar_recent_activity': recent_activity,
        'sidebar_workspace': current_workspace,
        'ai_credits_remaining': ai_credits_remaining,
        'ai_credits_budget': ai_credits_budget,
        'ai_credits_used': ai_credits_used,
        'ai_credits_percent_used': ai_credits_percent_used,
        'ai_credits_reset_at': ai_credits_reset_at,
        'ai_credits_low': ai_credits_low,
    }


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
    """Deterministic fallback suggestions when AI is unavailable. Covers all
    6 metrics, each driven by its category's assessment pillar score via
    GapAnalysisMetric.CATEGORY_PILLAR_MAPPING -- the same mapping used for
    score-movement tracking, so there's one source of truth for which
    pillar drives which metric instead of a second, ad-hoc lookup here.

    Known limitation, not a bug: there are only 3 real assessment pillars
    driving 6 metrics, so at least two metrics per pillar will always
    correlate -- a qualitative assessment can't produce 6 independent
    quantitative signals. The lower confidence score and the 'Formula
    Estimate' badge (see estimate_method) are what communicate that to the
    user; this function can't fix it with better math.
    """
    if not cat_scores:
        return []

    scores = {c['category'].name: float(c['avg']) for c in cat_scores}

    def clamp(value, lo, hi):
        return max(lo, min(hi, value))

    mapped = [
        {
            'category': 'Lead Generation',
            'metric': 'Monthly Qualified Leads',
            'base': 40, 'scale': 22, 'target_step': 18, 'min_value': 5, 'max_value': 400,
            'priority_if_below': 2.8,
            'recommendation': 'Tighten ICP filters, run weekly campaign reviews, and improve top-of-funnel messaging consistency.',
            'rationale': 'Demand score indicates lead quality/volume opportunity.',
            'higher_is_better': True,
        },
        {
            'category': 'Product Marketing',
            'metric': 'Product Qualified Leads',
            'base': 30, 'scale': 18, 'target_step': 15, 'min_value': 5, 'max_value': 350,
            'priority_if_below': 2.8,
            'recommendation': 'Improve free-trial onboarding and add in-app engagement triggers to surface product-qualified signals.',
            'rationale': 'Demand score indicates in-product engagement and qualification opportunity.',
            'higher_is_better': True,
        },
        {
            'category': 'Sales Efficiency',
            'metric': 'Win Rate',
            'base': 14, 'scale': 7, 'target_step': 8, 'min_value': 5, 'max_value': 80,
            'priority_if_below': 3.0,
            'recommendation': 'Standardize qualification, tighten discovery scripts, and run deal review coaching sessions.',
            'rationale': 'Conversion performance signals pipeline quality and sales process efficiency.',
            'higher_is_better': True,
        },
        {
            'category': 'Sales Velocity',
            'metric': 'Average Deal Size',
            'base': 8000, 'scale': 5500, 'target_step': 4000, 'min_value': 1000, 'max_value': 60000,
            'priority_if_below': 3.0,
            'recommendation': 'Focus on higher-value segments and value-based selling to lift average deal size.',
            'rationale': 'Conversion performance signals deal quality and pricing/positioning strength.',
            'higher_is_better': True,
        },
        {
            'category': 'Customer Success',
            'metric': 'Net Revenue Retention',
            'base': 75, 'scale': 8, 'target_step': 6, 'min_value': 50, 'max_value': 140,
            'priority_if_below': 3.2,
            'recommendation': 'Strengthen onboarding milestones, proactive success reviews, and expansion signal tracking.',
            'rationale': 'Delivery maturity drives retention and expansion reliability.',
            'higher_is_better': True,
        },
        {
            'category': 'Marketing ROI',
            'metric': 'CAC Payback Period',
            'base': 18, 'scale': -2.2, 'target_step': -2.0, 'min_value': 3, 'max_value': 36,
            'priority_if_below': 2.9,
            'recommendation': 'Reduce low-performing spend, improve conversion quality, and align campaign budgets to top channels.',
            'rationale': 'Acquisition efficiency can improve by optimizing spend-to-revenue cycle time.',
            'higher_is_better': False,
        },
    ]

    suggestions = []
    for item in mapped:
        pillar = GapAnalysisMetric.CATEGORY_PILLAR_MAPPING.get(item['category'])
        score = scores.get(pillar, 2.5)
        lo, hi = item['min_value'], item['max_value']

        current = clamp(item['base'] + score * item['scale'], lo, hi)
        target = current + item['target_step']
        if item['higher_is_better']:
            target = clamp(max(target, current + abs(item['target_step'])), lo, hi * 1.2)
        else:
            target = clamp(min(target, current - abs(item['target_step'])), lo * 0.5, hi)

        current = GapAnalysisMetric.round_metric_value(item['metric'], current)
        target = GapAnalysisMetric.round_metric_value(item['metric'], target)

        priority = 'High' if score < item['priority_if_below'] else 'Medium'
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
    account = None
    try:
        from gtm.ai_services import _get_client, _quota_cooldown_active, _request_budget_available
        from gtm.ai_credits import resolve_account_for_session
        account = resolve_account_for_session(session)
        if not _quota_cooldown_active() and _request_budget_available(account=account):
            client = _get_client()
    except Exception as exc:
        logger.warning('Vertex AI client unavailable: %s', exc)

    if not client:
        return _fallback_gap_suggestions(cat_scores), {
            'generator': 'fallback',
            'overall_score': round(float(overall), 1),
        }

    company_name = session.company_name or 'this company'
    industry = session.industry or 'an unspecified industry'
    company_size = session.company_size or 'an unspecified'
    company_stage = session.get_company_stage_display() if session.company_stage else 'an unspecified'

    prompt = f"""
You are a GTM analyst estimating rough operating benchmarks for a specific company from its GTM
readiness assessment. These are directional estimates for the team to sanity-check and refine
with their own real numbers -- not measured data, so do not overstate precision.

Company: {company_name}, {industry} industry, {company_size} employees, {company_stage}-stage.

Generate 3-5 actionable gap metric suggestions from this assessment.

Rules:
- Output STRICT JSON only (no markdown, no comments).
- Output shape: {{"suggestions": [{{"category":..., "metric":..., "current":..., "target":..., "priority":..., "recommendation":..., "rationale":..., "confidence":...}}]}}
- category must be one of: {allowed_categories}
- metric must be one of: {allowed_metrics}
- priority must be one of: {allowed_priorities}
- current/target must be plausible for a company of this size/industry/stage and directionally
  consistent with the category score (a lower score means further from target). Round to sensible
  whole-number precision -- do not invent decimal precision you have no grounds for.
- rationale must cite the SPECIFIC category score or weak-answer text that drove this number
  (e.g. "Demand scored 2.1/5, driven by 'no lead scoring in place'"), not generic advice.
- confidence must be an integer between 50 and 95, and should be lower when you're extrapolating
  loosely from a single category score rather than a specific weak answer.
- Keep recommendation under 180 chars.

Assessment context:
- Overall score: {round(float(overall), 1)}
- Category scores: {json.dumps(category_payload)}
- Weak answers: {json.dumps(weak_payload)}
""".strip()

    try:
        ai_response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        if hasattr(ai_response, 'usage_metadata'):
            from gtm.ai_credits import record_spend
            record_spend(account, ai_response.usage_metadata.total_token_count, "gap_suggestions", session=session)
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

        current = max(current, 0)
        target = max(target, 0)
        confidence = max(50, min(95, confidence))

        if metric == 'CAC Payback Period' and target >= current:
            target = max(1.0, current - 1.0)
        elif metric != 'CAC Payback Period' and target <= current:
            target = current + max(1.0, current * 0.1)

        # Enforce honest precision server-side regardless of what Gemini
        # actually returned -- never trust AI numeric output blindly.
        current = GapAnalysisMetric.round_metric_value(metric, current)
        target = GapAnalysisMetric.round_metric_value(metric, target)

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


MAX_ACTION_ITEMS_PER_GAP = 3


def _ai_action_items_for_gap_metric(metric, *, account=None):
    """Break a GapAnalysisMetric's standing recommendation into concrete,
    executable action items via one real Gemini call -- turning a static
    'here's what you should do' row into real Tasks-board items the AI
    completion pipeline can then act on. Falls back to a single
    deterministic item (the recommendation text itself) if AI is
    unavailable, so this never silently produces nothing."""
    fallback = [metric.recommendation[:240]] if metric.recommendation else [f"Address the {metric.metric} gap ({metric.category})"]

    if account is None:
        from gtm.ai_credits import resolve_account, resolve_account_for_session
        account = resolve_account(workspace=metric.workspace, user=metric.user) \
            or resolve_account_for_session(metric.session)

    client = None
    try:
        from gtm.ai_services import _get_client, _quota_cooldown_active, _request_budget_available
        if not _quota_cooldown_active() and _request_budget_available(account=account):
            client = _get_client()
    except Exception as exc:
        logger.warning('Vertex AI client unavailable: %s', exc)

    if not client:
        return fallback, {'generator': 'fallback'}

    prompt = f"""
You are a GTM operator turning one growth gap into concrete next steps.

Gap: {metric.category} — {metric.metric}
Current: {metric.current}   Target: {metric.target}   Priority: {metric.priority}
Standing recommendation: "{metric.recommendation}"

Break this into at most {MAX_ACTION_ITEMS_PER_GAP} concrete, specific action items a
team could actually start this week -- not vague restatements of the
recommendation. Each item is a single actionable task title, under 140 characters.

Output STRICT JSON only: {{"action_items": ["...", "..."]}}
""".strip()

    try:
        ai_response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        if hasattr(ai_response, 'usage_metadata'):
            from gtm.ai_credits import record_spend
            record_spend(account, ai_response.usage_metadata.total_token_count, "gap_action_items", session=metric.session)
        payload = json.loads(_clean_json_payload(getattr(ai_response, 'text', '')))
        items = payload.get('action_items', []) if isinstance(payload, dict) else []
    except Exception as exc:
        logger.warning('AI gap action-item generation failed; using fallback: %s', exc)
        return fallback, {'generator': 'fallback_after_ai_error'}

    cleaned = [item.strip()[:240] for item in items if isinstance(item, str) and item.strip()]
    if not cleaned:
        return fallback, {'generator': 'fallback_after_validation'}

    return cleaned[:MAX_ACTION_ITEMS_PER_GAP], {'generator': 'gemini'}


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
        generator = suggestion.source_payload.get('generator') if isinstance(suggestion.source_payload, dict) else None
        suggestion.is_formula_estimate = generator not in (None, 'gemini')
    return suggestions


def _gap_metric_scope_queryset(user, workspace, metric_name):
    """Return scope-aware queryset for a metric, used to enforce idempotent writes."""
    if workspace:
        return GapAnalysisMetric.objects.filter(workspace=workspace, metric=metric_name)
    return GapAnalysisMetric.objects.filter(workspace__isnull=True, user=user, metric=metric_name)


def estimate_method_from_generator(generator):
    """Map a GapAnalysisSuggestion.source_payload['generator'] value to the
    honest provenance label shown on the accepted metric row: real Gemini
    output vs. the deterministic fallback formula (any of the 'fallback*'
    generator variants)."""
    return 'gemini' if generator == 'gemini' else 'formula'


def _upsert_gap_metric_in_scope(*, user, workspace, session, payload, source='AI', estimate_method=''):
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
        existing.estimate_method = estimate_method
        existing.user = user
        if session:
            existing.session = session
        # Re-flagging a metric (new suggestion accepted / re-added manually)
        # reopens it -- a previously closed gap that got flagged again is a
        # fresh gap, not a closed one.
        existing.status = 'open'
        existing.closed_reason = ''
        existing.closed_at = None
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
        estimate_method=estimate_method,
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


def apply_measured_closure(metric):
    """Close a gap when a real, measured value has reached its target.

    Only call this after a genuine measurement updated metric.current
    (manual check-in now, CRM pull later) -- AI-estimated values must
    never close a gap. Direction-aware via _gap_percent_value (which
    already inverts CAC Payback Period, where lower is better).
    """
    if metric.status == 'closed':
        return False
    if _gap_percent_value(metric) < 0:
        return False
    metric.status = 'closed'
    metric.closed_reason = 'target_reached'
    metric.closed_at = timezone.now()
    metric.save(update_fields=['status', 'closed_reason', 'closed_at'])
    return True


def prepare_gap_metrics_for_display(metrics, workspace, user):
    """Annotate gap metrics with everything the table row renders: the
    existing gap%/priority styling, linked-task progress, deterministic
    pillar score movement against the latest completed assessment, and
    the retake-assessment hint. Single shared path for all render sites."""
    metrics = list(metrics)
    if not metrics:
        return metrics

    task_counts = {
        row['gap_metric_id']: row
        for row in ActionItem.objects.filter(gap_metric__in=metrics)
        .values('gap_metric_id')
        .annotate(
            total=models.Count('id'),
            done=models.Count('id', filter=models.Q(status='done')),
        )
    }

    if workspace:
        latest_session = AssessmentSession.objects.filter(
            workspace=workspace, is_completed=True,
        ).order_by('-created_at').first()
    else:
        latest_session = AssessmentSession.objects.filter(
            user=user, is_completed=True,
        ).order_by('-created_at').first()

    # _compute_scores per distinct session, cached across rows (max 6 rows/scope).
    score_cache = {}

    def _pillar_scores(session):
        if session.uuid not in score_cache:
            cat_scores, _overall = _compute_scores(session)
            score_cache[session.uuid] = {
                row['category'].name: float(row['avg']) for row in cat_scores
            }
        return score_cache[session.uuid]

    for metric in metrics:
        calculate_gap_metric_display_properties(metric)

        counts = task_counts.get(metric.id, {})
        metric.task_count = counts.get('total', 0)
        metric.done_task_count = counts.get('done', 0)

        metric.score_movement = None
        has_newer_assessment = bool(
            metric.session_id
            and latest_session
            and latest_session.uuid != metric.session_id
            and latest_session.created_at > metric.session.created_at
        )
        if has_newer_assessment:
            pillar = metric.pillar_name
            baseline = _pillar_scores(metric.session).get(pillar)
            latest = _pillar_scores(latest_session).get(pillar)
            if pillar and baseline is not None and latest is not None:
                metric.score_movement = {
                    'pillar': pillar,
                    'baseline': round(baseline, 1),
                    'latest': round(latest, 1),
                    'delta': round(latest - baseline, 1),
                }

        metric.suggest_reassessment = bool(
            metric.status == 'open'
            and metric.task_count > 0
            and metric.task_count == metric.done_task_count
            and not has_newer_assessment
        )

    return metrics


