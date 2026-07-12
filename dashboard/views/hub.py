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
    WorkspaceChatMessage,
)
from gtm.models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation, WorkspaceActivityEvent
from gtm.agent_runtime import AGENT_DIRECTORY, agent_display_label
from gtm.ai_chat import get_suggested_prompts, process_chat_message
from gtm.workspace_agent_chat import AGENT_TYPES, get_suggested_prompts_for_agent
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

    team_members = []
    workspace_memberships = []
    pending_invites = []
    accepted_awaiting = []

    if current_workspace:
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
    
    top_todo = action_items_qs.filter(status='todo').order_by('due_date').select_related('assigned_to', 'deliverable_document')
    top_doing = action_items_qs.filter(status='doing').order_by('due_date').select_related('assigned_to', 'deliverable_document')
    top_done = action_items_qs.filter(status='done').order_by('-created_at').select_related('assigned_to', 'deliverable_document')
    
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
    """Dedicated GTM agent page. Supports the original session-scoped GTM
    Agent ('session', default) plus three workspace-scoped agents (portfolio,
    resource, insights) selected via ?agent_type=."""
    workspace_id = request.GET.get('workspace') or request.session.get('current_workspace_id')
    agent_session_id = request.GET.get('agent_session')
    active_agent_type = request.GET.get('agent_type', 'session')
    if active_agent_type not in ('session', 'team') and active_agent_type not in AGENT_TYPES:
        active_agent_type = 'session'
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

    agent_session_options = []
    dashboard_agent_session = None
    dashboard_agent_prompts = []
    dashboard_agent_history = []
    workspace_agent_prompts = []
    workspace_agent_history = []
    team_agent_history = []
    team_selected_session = None

    if active_agent_type == 'team':
        from gtm.team_chat import _collect_team_timeline

        agent_session_options = [
            {
                'uuid': str(session.uuid),
                'label': f"{session.company_name or 'Unnamed'} • {session.created_at.strftime('%b %d, %Y')}",
            }
            for session in assessments_qs.order_by('-created_at')[:10]
        ]
        if agent_session_id and any(option['uuid'] == agent_session_id for option in agent_session_options):
            team_selected_session = assessments_qs.filter(uuid=agent_session_id).first()

        timeline = _collect_team_timeline(current_workspace, team_selected_session)
        for agent_type, created_at, message, response in timeline:
            team_agent_history.append({
                'agent_type': agent_type,
                'agent_label': agent_display_label(agent_type),
                'message': message,
                'response_html': _md(response or ''),
                'created_at': created_at,
            })
    elif active_agent_type == 'session':
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

        if agent_session_options:
            if agent_session_id and any(option['uuid'] == agent_session_id for option in agent_session_options):
                dashboard_agent_session = assessments_qs.filter(uuid=agent_session_id).first()
            if not dashboard_agent_session:
                dashboard_agent_session = assessments_qs.filter(uuid=agent_session_options[0]['uuid']).first()

        dashboard_agent_prompts = get_suggested_prompts(dashboard_agent_session) if dashboard_agent_session else []
        if dashboard_agent_session:
            history_qs = ChatMessage.objects.filter(session=dashboard_agent_session).order_by('-created_at')[:50]
            chats = list(reversed(history_qs))
            for chat in chats:
                chat.response_html = _md(chat.response or '')
            dashboard_agent_history = chats
    elif current_workspace:
        workspace_agent_prompts = get_suggested_prompts_for_agent(active_agent_type, current_workspace)
        history_qs = WorkspaceChatMessage.objects.filter(
            workspace=current_workspace, agent_type=active_agent_type
        ).order_by('-created_at')[:50]
        chats = list(reversed(history_qs))
        for chat in chats:
            chat.response_html = _md(chat.response or '')
        workspace_agent_history = chats

    # 'session' maps to the "gtm_strategist" AGENT_DIRECTORY entry; the 3
    # workspace agent tabs use their agent_type directly as the key.
    active_directory_key = 'gtm_strategist' if active_agent_type == 'session' else active_agent_type
    active_agent_info = AGENT_DIRECTORY.get(active_directory_key, {})
    active_agent_name = active_agent_info.get('name')
    active_agent_display = agent_display_label(active_directory_key) if active_agent_name else None

    team_names = [info['name'] for info in AGENT_DIRECTORY.values()]
    team_agent_names_display = ', '.join(team_names[:-1]) + f", and {team_names[-1]}" if len(team_names) > 1 else ''.join(team_names)

    context = {
        'current_workspace': current_workspace,
        'user_workspaces': user_workspaces,
        'active_agent_type': active_agent_type,
        'active_agent_name': active_agent_name,
        'active_agent_display': active_agent_display,
        'agent_tabs': [
            ('session', f"{AGENT_DIRECTORY['gtm_strategist']['name']} · GTM Agent"),
            ('portfolio', f"{AGENT_DIRECTORY['portfolio']['name']} · Portfolio"),
            ('resource', f"{AGENT_DIRECTORY['resource']['name']} · Resources"),
            ('insights', f"{AGENT_DIRECTORY['insights']['name']} · Insights"),
            ('team', 'Team'),
        ],
        'agent_display_labels': {at: agent_display_label(at) for at in AGENT_DIRECTORY},
        'agent_session_options': agent_session_options,
        'dashboard_agent_session': dashboard_agent_session,
        'dashboard_agent_prompts': dashboard_agent_prompts,
        'dashboard_agent_history': dashboard_agent_history,
        'workspace_agent_prompts': workspace_agent_prompts,
        'workspace_agent_history': workspace_agent_history,
        'team_agent_history': team_agent_history,
        'team_selected_session': team_selected_session,
        'team_agent_names_display': team_agent_names_display,
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
    
    # Check for onboarding flow (new user with 0 workspaces)
    if request.user.is_authenticated and not user_workspaces:
        context['show_onboarding'] = True

    # Asset Library stats for the snapshot widget
    if current_workspace:
        context['audited_asset_count'] = Resource.objects.filter(
            workspace=current_workspace, audit_status='complete'
        ).count()

    if request.htmx:
        response = render(request, 'dashboard/partials/dashboard_content.html', context)
        response['HX-Trigger'] = 'refreshNotifications, refreshAgentActions'
        return response

    return render(request, 'dashboard/home.html', context)


