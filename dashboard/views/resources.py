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
from ..parsers import log_workspace_activity

@login_required
@vary_on_headers('HX-Request')
def asset_library(request):
    """Full-page Library — workspace Resources (assets) and AI-authored
    Documents (client summaries, roadmaps, action-item deliverables) as two
    tabs over the same page. Resources and Documents are unrelated models
    that happen to share one browsing page now; each tab is independent."""
    current_workspace, user_workspaces = _resolve_dashboard_workspace(request)

    active_tab = request.GET.get('tab', 'resources')
    if active_tab not in ('resources', 'documents'):
        active_tab = 'resources'

    if current_workspace:
        resources = Resource.objects.filter(workspace=current_workspace).order_by('category', '-created_at')
    else:
        resources = Resource.objects.none()

    # Compute audit summary stats
    total = resources.count()
    audited = resources.filter(audit_status='complete').count()
    pending = resources.filter(audit_status__in=['pending', 'auditing']).count()

    document_groups = []
    total_documents = 0
    if current_workspace:
        from gtm.agent_documents import list_agent_documents
        from gtm.models import AgentDocument

        docs = list(
            list_agent_documents(workspace=current_workspace)
            .select_related('created_by')
            .prefetch_related('completed_action_items')
        )
        total_documents = len(docs)
        docs_by_type = {}
        for doc in docs:
            docs_by_type.setdefault(doc.doc_type, []).append(doc)
        for type_value, type_label in AgentDocument.DOC_TYPE_CHOICES:
            if type_value in docs_by_type:
                document_groups.append({'label': type_label, 'documents': docs_by_type[type_value]})

    context = {
        'current_workspace': current_workspace,
        'user_workspaces': user_workspaces,
        'active_tab': active_tab,
        'resources': resources,
        'total_assets': total,
        'audited_assets': audited,
        'pending_audits': pending,
        'document_groups': document_groups,
        'total_documents': total_documents,
        'page_title': 'Library',
    }

    if request.htmx:
        return render(request, 'dashboard/partials/asset_library_content.html', context)
    return render(request, 'dashboard/asset_library.html', context)


@require_http_methods(["POST"])
@login_required
def trigger_asset_audit(request, pk):
    """Fires an async AI audit for a single workspace Resource."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    resource = get_object_or_404(Resource, pk=pk, workspace=current_workspace)

    if resource.resource_type != 'file' or not resource.file:
        return JsonResponse({'error': 'This asset cannot be audited (not a file).'}, status=400)

    if resource.audit_status == 'auditing':
        return JsonResponse({'status': 'already_auditing'}, status=200)

    # Mark as pending, then fire background thread
    resource.audit_status = 'pending'
    resource.save(update_fields=['audit_status'])

    from gtm.ai_auditor import audit_resource_async
    audit_resource_async(resource.pk)

    # Return the "auditing" card state via HTMX
    return render(request, 'dashboard/partials/asset_card.html', {
        'resource': resource,
        'current_workspace': current_workspace,
    })


@require_http_methods(["GET"])
@login_required
def asset_audit_result(request, pk):
    """Polling endpoint — returns the current audit state for a single resource card."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    resource = get_object_or_404(Resource, pk=pk, workspace=current_workspace)
    # Refresh from DB to get latest audit_status
    resource.refresh_from_db()

    return render(request, 'dashboard/partials/asset_card.html', {
        'resource': resource,
        'current_workspace': current_workspace,
    })


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
                # Return updated asset library with all stats recalculated
                context = {
                    'current_workspace': current_workspace,
                    'user_workspaces': current_workspace.members.filter(user=request.user).values_list('workspace', flat=True) if hasattr(current_workspace, 'members') else [],
                    'resources': Resource.objects.filter(workspace=current_workspace),
                    'total_assets': Resource.objects.filter(workspace=current_workspace).count(),
                    'audited_assets': Resource.objects.filter(workspace=current_workspace, audit_status='complete').count(),
                    'pending_audits': Resource.objects.filter(workspace=current_workspace, audit_status__in=['pending', 'auditing']).count(),
                }
                response = render(request, 'dashboard/partials/asset_library_content.html', context)
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

        # Get workspace before deleting the resource
        current_workspace = resource.workspace
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
            # Return updated asset library with all stats recalculated
            context = {
                'current_workspace': current_workspace,
                'user_workspaces': current_workspace.members.filter(user=request.user).values_list('workspace', flat=True) if hasattr(current_workspace, 'members') else [],
                'resources': Resource.objects.filter(workspace=current_workspace),
                'total_assets': Resource.objects.filter(workspace=current_workspace).count(),
                'audited_assets': Resource.objects.filter(workspace=current_workspace, audit_status='complete').count(),
                'pending_audits': Resource.objects.filter(workspace=current_workspace, audit_status__in=['pending', 'auditing']).count(),
            }
            response = render(request, 'dashboard/partials/asset_library_content.html', context)
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

