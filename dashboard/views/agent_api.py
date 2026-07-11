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

    # [VISION BRIDGE] Save strategic evidence files to GTMFile
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

    # [DEEP CLEAR] Wipe chat messages AND strategic evidence files
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

