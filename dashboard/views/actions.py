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
from ..parsers import log_workspace_activity

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
                
                # Notify assignee
                if instance.assigned_to and instance.assigned_to != request.user:
                    send_notification(
                        recipient=instance.assigned_to,
                        sender=request.user,
                        workspace=current_workspace,
                        notification_type='task',
                        level='info',
                        title="New Task Assigned",
                        message=f"You have been assigned a new task: {instance.note[:50]}...",
                        link=f"/dashboard/tasks/?workspace={current_workspace.id if current_workspace else ''}"
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



