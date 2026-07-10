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

from .helpers import (
    _ai_gap_suggestions_from_assessment,
    _build_recent_agent_actions,
    _build_sidebar_notifications_context,
    _clean_json_payload,
    _collect_recent_action_feed,
    _fallback_gap_suggestions,
    _gap_metric_scope_queryset,
    _gap_percent_value,
    _get_gap_scope,
    _load_pending_gap_suggestions,
    _md,
    _normalize_action_text,
    _resolve_dashboard_workspace,
    _upsert_gap_metric_in_scope,
)

@require_http_methods(["GET"])
@login_required
def notifications_panel(request):
    """Render the right sidebar notifications/activity panel."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    context = _build_sidebar_notifications_context(request, current_workspace)
    return render(request, 'dashboard/partials/notifications_panel.html', context)


# ================================================================
# STRATEGIC ASSET LIBRARY VIEWS
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

@login_required
def create_workspace_dashboard(request):
    """Create workspace from dashboard and return modal partial for HTMX."""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        is_onboarding = request.POST.get('onboarding') == 'true'
        
        if name:
            workspace = Workspace.create_for_user(name=name, user=request.user)
            # On success, trigger dashboard refresh or close modal via HTMX
            if is_onboarding:
                return render(request, 'dashboard/partials/onboarding_overlay.html', {
                    'success': True,
                    'workspace': workspace,
                })
            
            return render(request, 'dashboard/partials/workspace_create_modal.html', {
                'success': True,
                'workspace': workspace,
            })
        # If error, re-render modal with error message
        template = 'dashboard/partials/onboarding_overlay.html' if is_onboarding else 'dashboard/partials/workspace_create_modal.html'
        return render(request, template, {
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


@vary_on_headers('HX-Request')
@login_required
def settings_view(request):
    user_settings, _ = UserSettings.objects.get_or_create(user=request.user)

    if request.method == 'POST':
        form = UserSettingsForm(request.POST, instance=user_settings)
        if form.is_valid():
            form.save()
            messages.success(request, 'Settings saved successfully.')
            ctx = {'form': form, 'success': True}
        else:
            ctx = {'form': form, 'success': False}
    else:
        form = UserSettingsForm(instance=user_settings)
        ctx = {'form': form, 'success': False}

    template = 'dashboard/partials/settings_content.html' if request.htmx else 'dashboard/settings.html'
    return render(request, template, ctx)
