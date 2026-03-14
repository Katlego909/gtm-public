# dashboard/views.py
"""
Dashboard views for GTM Validator
"""

import datetime
import json
import markdown
import re
import uuid
import logging

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
)
from gtm.models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation, WorkspaceActivityEvent
from gtm.ai_chat import get_suggested_prompts, process_chat_message
from gtm.decorators import workspace_permission_required, workspace_admin_required, workspace_member_required
from dashboard.models import Channel, ChannelAnalytics, GapAnalysisMetric, GapAnalysisSuggestion, Resource
from .forms import GapAnalysisMetricForm, ActionItemForm, UserProfileForm, ResourceForm
from gtm.views import _compute_scores, _band_for_score
from .utils import calculate_gap_metric_display_properties

logger = logging.getLogger(__name__)

# ================================================================
# RESOURCE LIBRARY VIEWS
# ================================================================


def log_workspace_activity(workspace, actor, event_type, summary, object_type='', object_id='', metadata=None, session=None):
    """Create a workspace activity event if workspace context is available."""
    if not workspace:
        return
    WorkspaceActivityEvent.objects.create(
        workspace=workspace,
        session=session,
        actor=actor if getattr(actor, 'is_authenticated', False) else None,
        event_type=event_type,
        summary=summary,
        object_type=object_type or '',
        object_id=str(object_id) if object_id else '',
        metadata=metadata or {},
    )


def _extract_create_task_command(message):
    """Parse simple dashboard action commands like: add a task called "X" and assign to Y."""
    if not message:
        return None

    text = message.strip()

    quoted_pattern = re.compile(
        r"^(?:add|create)\s+(?:a\s+)?task(?:\s+(?:called|named))?\s+[\"'](?P<title>[^\"']+)[\"'](?:\s+and\s+assign(?:\s+it)?\s+to\s+(?P<assignee>.+))?$",
        re.IGNORECASE,
    )
    plain_pattern = re.compile(
        r"^(?:add|create)\s+(?:a\s+)?task(?:\s+(?:called|named))?\s+(?P<title>[^\n,.]+?)(?:\s+and\s+assign(?:\s+it)?\s+to\s+(?P<assignee>[^\n,.]+))?$",
        re.IGNORECASE,
    )

    match = quoted_pattern.match(text) or plain_pattern.match(text)
    if not match:
        return None

    title = (match.group('title') or '').strip().strip('"\'')
    assignee = (match.group('assignee') or '').strip().strip('"\'')
    if not title:
        return None

    return {
        'action': 'create_task',
        'title': title[:240],
        'assignee': assignee,
    }


def _normalize_task_status(raw_status):
    if not raw_status:
        return None
    cleaned = raw_status.strip().lower().replace('-', ' ').replace('_', ' ')
    mapping = {
        'todo': 'todo',
        'to do': 'todo',
        'backlog': 'todo',
        'doing': 'doing',
        'in progress': 'doing',
        'progress': 'doing',
        'done': 'done',
        'complete': 'done',
        'completed': 'done',
        'finish': 'done',
        'finished': 'done',
    }
    return mapping.get(cleaned)


def _extract_move_task_command(message):
    if not message:
        return None
    text = message.strip()

    patterns = [
        re.compile(
            r"^(?:move|set|update|change)\s+task\s+(?P<target>.+?)\s+(?:to|as)\s+(?P<status>todo|to do|doing|in progress|done|complete|completed|finished)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:move|set|update|change)\s+[\"'](?P<target>[^\"']+)[\"']\s+(?:to|as)\s+(?P<status>todo|to do|doing|in progress|done|complete|completed|finished)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:mark)\s+task\s+(?P<target>.+?)\s+(?:as\s+)?(?P<status>done|complete|completed|finished|doing|in progress|todo|to do)$",
            re.IGNORECASE,
        ),
    ]

    match = None
    for pattern in patterns:
        match = pattern.match(text)
        if match:
            break
    if not match:
        return None

    status = _normalize_task_status(match.group('status'))
    target = (match.group('target') or '').strip().strip('"\'')
    if not target or not status:
        return None

    return {
        'action': 'move_task',
        'target': target,
        'status': status,
    }


def _extract_delete_task_command(message):
    if not message:
        return None
    text = message.strip()

    patterns = [
        re.compile(r"^(?:delete|remove)\s+task\s+[\"'](?P<target>[^\"']+)[\"']$", re.IGNORECASE),
        re.compile(r"^(?:delete|remove)\s+task\s+(?P<target>.+)$", re.IGNORECASE),
    ]
    match = None
    for pattern in patterns:
        match = pattern.match(text)
        if match:
            break
    if not match:
        return None

    target = (match.group('target') or '').strip().strip('"\'')
    return {'action': 'delete_task', 'target': target} if target else None


def _extract_comment_task_command(message):
    if not message:
        return None
    text = message.strip()

    patterns = [
        re.compile(
            r"^(?:add\s+)?comment\s+(?:on|to)\s+task\s+(?P<target>.+?)\s*:\s*(?P<comment>.+)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:add\s+)?comment\s+[\"'](?P<comment>[^\"']+)[\"']\s+(?:on|to)\s+task\s+(?P<target>.+)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"^(?:add\s+)?note\s+(?:on|to)\s+task\s+(?P<target>.+?)\s*:\s*(?P<comment>.+)$",
            re.IGNORECASE,
        ),
    ]
    match = None
    for pattern in patterns:
        match = pattern.match(text)
        if match:
            break
    if not match:
        return None

    target = (match.group('target') or '').strip().strip('"\'')
    comment = (match.group('comment') or '').strip().strip('"\'')
    if not target or not comment:
        return None

    return {
        'action': 'comment_task',
        'target': target,
        'comment': comment[:500],
    }


def _resolve_assignee(current_workspace, assignee_text):
    """Resolve assignee name/email to a user in the current workspace."""
    if not current_workspace or not assignee_text:
        return None

    candidate = assignee_text.strip()
    memberships = WorkspaceMembership.objects.filter(
        workspace=current_workspace,
        is_active=True,
    ).select_related('user')

    for membership in memberships:
        user = membership.user
        full_name = (user.get_full_name() or '').strip().lower()
        username = (user.username or '').strip().lower()
        email = (user.email or '').strip().lower()
        check = candidate.lower()
        if check in {full_name, username, email}:
            return user

    matches = memberships.filter(
        models.Q(user__first_name__icontains=candidate)
        | models.Q(user__last_name__icontains=candidate)
        | models.Q(user__username__icontains=candidate)
        | models.Q(user__email__icontains=candidate)
    )
    membership = matches.first()
    return membership.user if membership else None


def _task_command_queryset(request, session, current_workspace):
    if current_workspace:
        return ActionItem.objects.filter(workspace=current_workspace)
    return ActionItem.objects.filter(
        workspace__isnull=True,
        session__user=request.user,
    )


def _resolve_task_for_command(request, session, current_workspace, target):
    """Resolve a task by id or title within current workspace/personal scope."""
    base_qs = _task_command_queryset(request, session, current_workspace)
    token = (target or '').strip().strip('"\'')
    if not token:
        return None

    if token.isdigit():
        return base_qs.filter(pk=int(token)).first()

    by_exact = base_qs.filter(note__iexact=token).order_by('-updated_at')
    if by_exact.exists():
        return by_exact.first()

    session_first = base_qs.filter(session=session, note__icontains=token).order_by('-updated_at')
    if session_first.exists():
        return session_first.first()

    return base_qs.filter(note__icontains=token).order_by('-updated_at').first()


def _run_dashboard_action_command(request, session, current_workspace, message):
    """Execute deterministic dashboard commands before free-form AI chat."""
    cmd = (
        _extract_create_task_command(message)
        or _extract_move_task_command(message)
        or _extract_delete_task_command(message)
        or _extract_comment_task_command(message)
    )
    if not cmd:
        return None

    actor_name = request.user.get_full_name() or request.user.username

    if cmd['action'] == 'create_task':
        assigned_user = _resolve_assignee(current_workspace, cmd['assignee'])
        item = ActionItem.objects.create(
            session=session,
            workspace=current_workspace,
            note=cmd['title'],
            status='todo',
            created_by=request.user,
            assigned_to=assigned_user,
            owner=(assigned_user.get_full_name() if assigned_user else actor_name),
        )

        assignee_text = assigned_user.get_full_name() or assigned_user.username if assigned_user else None
        if current_workspace:
            summary = (
                f"{actor_name} created task '{item.note}'"
                + (f" and assigned it to {assignee_text}." if assignee_text else ".")
            )
            log_workspace_activity(
                current_workspace,
                request.user,
                'task_created',
                summary,
                object_type='action_item',
                object_id=item.id,
                metadata={'source': 'dashboard_agent', 'assigned_to': assignee_text or ''},
                session=item.session,
            )

        if cmd['assignee'] and not assigned_user and current_workspace:
            response_text = (
                f"✅ Task created: **{item.note}** (To do).\n"
                f"I could not find **{cmd['assignee']}** in this workspace, so it is currently unassigned."
            )
        elif assignee_text:
            response_text = f"✅ Task created: **{item.note}** and assigned to **{assignee_text}**."
        else:
            response_text = f"✅ Task created: **{item.note}** (To do)."

        return {
            'success': True,
            'response': response_text,
            'intent': 'dashboard_action',
            'action': {
                'type': 'create_task',
                'task_id': item.id,
                'assigned_to': assignee_text,
            }
        }

    task = _resolve_task_for_command(request, session, current_workspace, cmd.get('target'))
    if not task:
        return {
            'success': True,
            'response': "I could not find that task in your current workspace context. Try using the task ID or exact title.",
            'intent': 'dashboard_action',
            'action': {'type': 'not_found'},
        }

    if cmd['action'] == 'move_task':
        if current_workspace:
            membership = WorkspaceMembership.objects.filter(
                user=request.user,
                workspace=current_workspace,
                is_active=True,
            ).first()
            if not membership or not membership.can_assign_tasks:
                return {
                    'success': True,
                    'response': "You do not have permission to move tasks in this workspace.",
                    'intent': 'dashboard_action',
                    'action': {'type': 'permission_denied'},
                }

        task.status = cmd['status']
        task.save(update_fields=['status', 'updated_at'])

        if current_workspace:
            log_workspace_activity(
                current_workspace,
                request.user,
                'task_moved',
                f"{actor_name} moved task '{task.note[:80]}' to {task.get_status_display()}.",
                object_type='action_item',
                object_id=task.id,
                metadata={'source': 'dashboard_agent', 'status': task.status},
                session=task.session,
            )

        return {
            'success': True,
            'response': f"✅ Moved **{task.note}** to **{task.get_status_display()}**.",
            'intent': 'dashboard_action',
            'action': {'type': 'move_task', 'task_id': task.id, 'status': task.status},
        }

    if cmd['action'] == 'delete_task':
        task_id = task.id
        task_note = task.note
        task_session = task.session
        task.delete()

        if current_workspace:
            log_workspace_activity(
                current_workspace,
                request.user,
                'task_deleted',
                f"{actor_name} deleted task '{task_note[:80]}'.",
                object_type='action_item',
                object_id=task_id,
                metadata={'source': 'dashboard_agent'},
                session=task_session,
            )

        return {
            'success': True,
            'response': f"🗑️ Deleted task **{task_note}**.",
            'intent': 'dashboard_action',
            'action': {'type': 'delete_task', 'task_id': task_id},
        }

    if cmd['action'] == 'comment_task':
        comment = ActionItemComment.objects.create(
            action_item=task,
            user=request.user,
            text=cmd['comment'],
        )

        if current_workspace:
            log_workspace_activity(
                current_workspace,
                request.user,
                'comment_added',
                f"{actor_name} commented on task '{task.note[:80]}'.",
                object_type='action_item',
                object_id=task.id,
                metadata={'source': 'dashboard_agent', 'comment_id': str(comment.id)},
                session=task.session,
            )

        return {
            'success': True,
            'response': f"💬 Added comment to **{task.note}**.",
            'intent': 'dashboard_action',
            'action': {'type': 'comment_task', 'task_id': task.id, 'comment_id': str(comment.id)},
        }

    return None


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
        return render(request, 'dashboard/partials/workspace_hub_content.html', context)
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
        return render(request, 'dashboard/partials/tasks_content.html', context)
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
        dashboard_agent_history = list(reversed(history_qs))

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
        return render(request, 'dashboard/partials/agent_content.html', context)
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
    
    # Filter data by workspace if selected
    if current_workspace:
        # Workspace-scoped data
        assessments_qs = AssessmentSession.objects.filter(workspace=current_workspace)
        action_items_qs = ActionItem.objects.filter(workspace=current_workspace).select_related('assigned_to', 'session')
    else:
        # Fallback to user's data (legacy support)
        assessments_qs = AssessmentSession.objects.filter(user=request.user) if request.user.is_authenticated else AssessmentSession.objects.none()
        action_items_qs = ActionItem.objects.filter(session__user=request.user).select_related('assigned_to', 'session') if request.user.is_authenticated else ActionItem.objects.none()

    # Recent validator results for dashboard
    if current_workspace:
        validator_results = ResultSnapshot.objects.filter(session__workspace=current_workspace).select_related('session', 'band').order_by('-created_at')[:10]
    else:
        validator_results = ResultSnapshot.objects.filter(session__user=request.user).select_related('session', 'band').order_by('-created_at')[:10] if request.user.is_authenticated else []

    # Weekly Activity: Sessions, Items Created, Items Completed per day (last 7 days)
    today = timezone.now().date()
    days = [(today - datetime.timedelta(days=i)) for i in range(6, -1, -1)]
    weekly_activity = []
    for day in days:
        sessions = assessments_qs.filter(created_at__date=day).count()
        items_created = action_items_qs.filter(created_at__date=day).count()
        if 'completed_at' in [f.name for f in ActionItem._meta.fields]:
            items_completed = action_items_qs.filter(status='done', completed_at__date=day).count()
        else:
            items_completed = action_items_qs.filter(status='done', created_at__date=day).count()
        weekly_activity.append({
            'date': day.strftime('%Y-%m-%d'),
            'label': day.strftime('%a'),
            'sessions': sessions,
            'items_created': items_created,
            'items_completed': items_completed
        })

    # KPIs with percentage changes (now workspace-scoped)
    total_sessions = assessments_qs.count()
    completed_items = action_items_qs.filter(status='done').count()
    pending_items = action_items_qs.exclude(status='done').count()
    
    # Calculate percentage changes (compare this week vs last week)
    this_week_start = today - datetime.timedelta(days=today.weekday())
    last_week_start = this_week_start - datetime.timedelta(days=7)
    last_week_end = this_week_start - datetime.timedelta(days=1)
    
    # Sessions this week vs last week (workspace-scoped)
    this_week_sessions = assessments_qs.filter(
        created_at__date__gte=this_week_start
    ).count()
    last_week_sessions = assessments_qs.filter(
        created_at__date__gte=last_week_start,
        created_at__date__lte=last_week_end
    ).count()
    
    # Calculate percentage change for sessions
    sessions_change = 0
    sessions_change_positive = True
    sessions_has_meaningful_change = False
    if last_week_sessions > 0:
        sessions_change = round(((this_week_sessions - last_week_sessions) / last_week_sessions) * 100)
        sessions_change_positive = sessions_change >= 0
        sessions_has_meaningful_change = sessions_change != 0
    # Don't show percentage for 0 -> anything transitions
    
    # Completed items this week vs last week (workspace-scoped)
    # Use updated_at so moving an existing task to Done this week is counted.
    this_week_completed = action_items_qs.filter(
        status='done',
        updated_at__date__gte=this_week_start
    ).count()
    last_week_completed = action_items_qs.filter(
        status='done',
        updated_at__date__gte=last_week_start,
        updated_at__date__lte=last_week_end
    ).count()
    
    # Calculate percentage change for completed items
    completed_change = 0
    completed_change_positive = True
    completed_has_meaningful_change = False
    if last_week_completed > 0:
        completed_change = round(((this_week_completed - last_week_completed) / last_week_completed) * 100)
        completed_change_positive = completed_change >= 0
        completed_has_meaningful_change = completed_change != 0
    elif this_week_completed > 0:
        # Zero-baseline transition: show visible movement instead of "No change this week".
        completed_change = 100
        completed_change_positive = True
        completed_has_meaningful_change = True
    # Otherwise both weeks are zero -> no meaningful change.
        
    # Pending items this week vs last week (workspace-scoped)
    this_week_pending = action_items_qs.filter(
        status__in=['todo', 'doing'], 
        created_at__date__gte=this_week_start
    ).count()
    last_week_pending = action_items_qs.filter(
        status__in=['todo', 'doing'],
        created_at__date__gte=last_week_start,
        created_at__date__lte=last_week_end
    ).count()
    
    # Calculate percentage change for pending items (negative is good)
    pending_change = 0
    pending_change_positive = False  # For pending items, decrease is positive
    pending_has_meaningful_change = False
    if last_week_pending > 0:
        raw_change = ((this_week_pending - last_week_pending) / last_week_pending) * 100
        pending_change = round(abs(raw_change))
        pending_change_positive = raw_change < 0  # Decrease in pending is good
        pending_has_meaningful_change = pending_change != 0
    recent_items = action_items_qs.order_by('-created_at')[:8]

    # Chart data: Sessions per day (last 7 days) - workspace-scoped
    sessions_per_day = [assessments_qs.filter(created_at__date=day).count() for day in days]
    days_labels = [day.strftime('%a') for day in days]

    # Status breakdown for donut chart
    status_breakdown = {
        'completed': completed_items,
        'pending': pending_items
    }

    # Top action items by status (workspace-scoped)
    top_todo = action_items_qs.filter(status='todo').order_by('due_date')
    top_doing = action_items_qs.filter(status='doing').order_by('due_date')
    top_done = action_items_qs.filter(status='done').order_by('-created_at')

    # Tool recommendations: only show if there are assessments in workspace
    if assessments_qs.exists():
        top_tools_qs = ToolRecommendation.objects.all()[:5]
        top_tools = []
        for tool in top_tools_qs:
            if tool.tools:
                tool.tools_list = [t.strip().lower().title() for t in tool.tools.split(',')]
            else:
                tool.tools_list = []
            top_tools.append(tool)
    else:
        top_tools = []

    # Insights (from ResultSnapshot.ai_playbook) - WORKSPACE-SCOPED
    if current_workspace:
        insights_qs = ResultSnapshot.objects.filter(
            session__workspace=current_workspace
        ).exclude(ai_playbook="").order_by('-created_at')[:5]
    else:
        insights_qs = ResultSnapshot.objects.filter(
            session__user=request.user
        ).exclude(ai_playbook="").order_by('-created_at')[:5] if request.user.is_authenticated else ResultSnapshot.objects.none()
    
    insights = []
    for insight in insights_qs:
        insight.playbook_html = markdown.markdown(insight.ai_playbook or "")
        insights.append(insight)

    # Recent chat messages - WORKSPACE-SCOPED
    if current_workspace:
        recent_chats_qs = ChatMessage.objects.filter(
            session__workspace=current_workspace
        )
    else:
        recent_chats_qs = ChatMessage.objects.filter(
            session__user=request.user
        ) if request.user.is_authenticated else ChatMessage.objects.none()

    recent_agent_actions = _collect_recent_action_feed(request, current_workspace, max_items=6)

    # GTM Assessment History & Trends (workspace-scoped)
    assessment_history = []
    assessment_stats = None
    score_trend_data = {'labels': [], 'scores': []}
    agent_session_options = []
    dashboard_agent_session = None
    dashboard_agent_prompts = []
    dashboard_agent_history = []
    
    if request.user.is_authenticated:
        # Get completed assessments for the workspace (or user if no workspace)
        completed_sessions = assessments_qs.filter(
            is_completed=True
        ).order_by('-created_at')[:10]
        
        all_scores = []
        for session in completed_sessions:
            cat_scores, overall = _compute_scores(session)
            band = _band_for_score(overall)
            assessment_history.append({
                'session': session,
                'uuid': session.uuid,
                'company_name': session.company_name or 'Unnamed',
                'industry': session.industry or 'N/A',
                'overall_score': round(overall, 1),
                'band_stage': band.stage if band else 'Unknown',
                'created_at': session.created_at,
            })
            all_scores.append(overall)
        
        # Calculate statistics
        if all_scores:
            total_assessments = len(all_scores)
            avg_score = sum(all_scores) / len(all_scores)
            improvement = 0
            if len(all_scores) >= 2:
                improvement = all_scores[0] - all_scores[-1]  # Latest - First
            
            assessment_stats = {
                'total': total_assessments,
                'average': round(avg_score, 1),
                'improvement': round(improvement, 1),
                'latest_score': round(all_scores[0], 1) if all_scores else 0,
            }
            
            # Prepare trend chart data (reverse to show chronological order)
            score_trend_data = {
                'labels': [s['created_at'].strftime('%m/%d') for s in reversed(assessment_history)],
                'scores': list(reversed(all_scores))
            }

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

        if agent_session_options:
            if agent_session_id and any(option['uuid'] == agent_session_id for option in agent_session_options):
                dashboard_agent_session = assessments_qs.filter(uuid=agent_session_id).first()
            if not dashboard_agent_session:
                dashboard_agent_session = assessments_qs.filter(uuid=agent_session_options[0]['uuid']).first()

            if dashboard_agent_session:
                dashboard_agent_prompts = get_suggested_prompts(dashboard_agent_session)
                history_qs = ChatMessage.objects.filter(session=dashboard_agent_session).order_by('-created_at')[:30]
                dashboard_agent_history = list(reversed(history_qs))

    # Convert markdown notes to HTML for all relevant items
    def convert_notes(items):
        for item in items:
            item.note_html = markdown.markdown(item.note or "")
        return items

    recent_items = convert_notes(list(recent_items))

    # Channel analytics for donut chart
    channels = Channel.objects.all()
    analytics_qs = ChannelAnalytics.objects.filter(date=today)
    total_revenue = sum(a.revenue for a in analytics_qs)
    channel_data = []
    for channel in channels:
        analytics = analytics_qs.filter(channel=channel).first()
        if analytics and total_revenue > 0:
            percent = (analytics.revenue / total_revenue) * 100
            channel_data.append({
                "name": channel.name,
                "percent": round(percent, 2),
                "change": analytics.change,
                "color": channel.color,
            })

    # --- Gap Analysis Logic --- (workspace-scoped)
    if current_workspace:
        gap_analysis = GapAnalysisMetric.objects.filter(
            workspace=current_workspace
        ).order_by('category', 'priority')
        latest_completed_gap_session = AssessmentSession.objects.filter(
            workspace=current_workspace,
            is_completed=True,
        ).order_by('-created_at').first()
    else:
        gap_analysis = GapAnalysisMetric.objects.filter(
            user=request.user, workspace__isnull=True
        ).order_by('category', 'priority') if request.user.is_authenticated else GapAnalysisMetric.objects.none()
        latest_completed_gap_session = AssessmentSession.objects.filter(
            user=request.user,
            is_completed=True,
        ).order_by('-created_at').first() if request.user.is_authenticated else None
    for metric in gap_analysis:
        calculate_gap_metric_display_properties(metric)

    gap_suggestions = _load_pending_gap_suggestions(request, current_workspace) if request.user.is_authenticated else []

    if current_workspace:
        gap_report_url = f"{reverse('gap_report')}?workspace={current_workspace.id}"
    else:
        gap_report_url = reverse('gap_report')
    
    context = {
        'total_sessions': total_sessions,
        'completed_items': completed_items,
        'pending_items': pending_items,
        'sessions_change': sessions_change,
        'sessions_change_positive': sessions_change_positive,
        'sessions_has_meaningful_change': sessions_has_meaningful_change,
        'completed_change': completed_change,
        'completed_change_positive': completed_change_positive,
        'completed_has_meaningful_change': completed_has_meaningful_change,
        'pending_change': pending_change,
        'pending_change_positive': pending_change_positive,
        'pending_has_meaningful_change': pending_has_meaningful_change,
        'recent_items': recent_items,
        'sessions_per_day': sessions_per_day,
        'days_labels': days_labels,
        'status_breakdown': status_breakdown,
        'top_tools': top_tools,
        'insights': insights,
        'recent_agent_actions': recent_agent_actions,
        'weekly_activity': weekly_activity,
        'validator_results': validator_results,
        'channel_data': channel_data,
        'gap_analysis': gap_analysis,
        'gap_suggestions': gap_suggestions,
        'latest_completed_gap_session': latest_completed_gap_session,
        'gap_report_url': gap_report_url,
        'assessment_history': assessment_history,
        'assessment_stats': assessment_stats,
        'score_trend_data': score_trend_data,
        'agent_session_options': agent_session_options,
        'dashboard_agent_session': dashboard_agent_session,
        'dashboard_agent_prompts': dashboard_agent_prompts,
        'dashboard_agent_history': dashboard_agent_history,
        # Workspace context
        'current_workspace': current_workspace,
        'user_workspaces': user_workspaces,
    }

    if request.htmx:
        return render(request, 'dashboard/partials/dashboard_content.html', context)

    return render(request, 'dashboard/home.html', context)


@require_http_methods(["POST"])
@login_required
def dashboard_agent_api(request):
    """Run the GTM agent inline from the dashboard without navigating to chat."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    session_id = str(data.get('session_id', '')).strip()
    message = (data.get('message') or '').strip()

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

    action_result = _run_dashboard_action_command(request, session, current_workspace, message)
    if action_result:
        ChatMessage.objects.create(
            session=session,
            user=request.user,
            message=message,
            response=action_result.get("response", ""),
            intent=action_result.get("intent", "dashboard_action"),
        )
        return JsonResponse(action_result, status=200)

    result = process_chat_message(
        session_id=str(session.uuid),
        message=message,
        user=request.user,
    )

    if result.get("success"):
        ChatMessage.objects.create(
            session=session,
            user=request.user,
            message=message,
            response=result.get("response", ""),
            intent=result.get("intent", ""),
        )

    return JsonResponse(result, status=200 if result.get("success") else 500)


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
            current_workspace = next(w for w in user_workspaces if str(w.id) == workspace_id)
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
    """Create workspace from dashboard and return updated dashboard view."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            workspace = Workspace.create_for_user(name=name, user=request.user)
            # Redirect to dashboard with new workspace selected
            return redirect(f'/dashboard/?workspace={workspace.id}')
        
    return redirect('/dashboard/')


@login_required
def invite_to_workspace(request, workspace_id):
    """Handle team invitations from the dashboard."""
    workspace = get_object_or_404(Workspace, id=workspace_id)
    dashboard_url = f'/dashboard/?workspace={workspace_id}'

    # Permission check
    membership = WorkspaceMembership.objects.filter(workspace=workspace, user=request.user).first()
    if not membership or membership.role not in ['admin', 'manager']:
        messages.error(request, "You don't have permission to invite members.")
        return redirect(dashboard_url)

    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        role = request.POST.get('role', 'contributor')

        if not email:
            messages.error(request, 'Email address is required.')
            return redirect(dashboard_url)

        # Prevent duplicate pending invitations
        if WorkspaceInvitation.objects.filter(workspace=workspace, email__iexact=email, accepted_at__isnull=True).exists():
            messages.warning(request, f'An invitation to {email} is already pending.')
            return redirect(dashboard_url)

        # Check if already a member
        if WorkspaceMembership.objects.filter(workspace=workspace, user__email__iexact=email).exists():
            messages.warning(request, f'{email} is already a member of this workspace.')
            return redirect(dashboard_url)

        invitation = WorkspaceInvitation.objects.create(
            workspace=workspace,
            email=email,
            role=role,
            invited_by=request.user,
        )

        try:
            from gtm.utils_email import send_workspace_invitation_email
            send_workspace_invitation_email(invitation, request)
            messages.success(request, f'Invitation sent successfully to {email}!')
        except Exception as e:
            invitation.delete()
            messages.error(request, f'Failed to send invitation email: {e}')

    return redirect(dashboard_url)
