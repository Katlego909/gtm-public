# dashboard/views.py
"""
Dashboard views for GTM Validator
"""

import datetime
import json
import markdown
import uuid

from django.shortcuts import get_object_or_404, render, redirect
from django.db import models
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
from gtm.models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation
from gtm.ai_chat import get_suggested_prompts, process_chat_message
from gtm.decorators import workspace_permission_required, workspace_admin_required, workspace_member_required
from dashboard.models import Channel, ChannelAnalytics, GapAnalysisMetric, Resource
from .forms import GapAnalysisMetricForm, ActionItemForm, UserProfileForm, ResourceForm
from gtm.views import _compute_scores, _band_for_score
from .utils import calculate_gap_metric_display_properties

# ================================================================
# RESOURCE LIBRARY VIEWS
# ================================================================

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
        resource.delete()
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
            instance = form.save(commit=False)
            # Auto-assign workspace and user for new metrics
            if not instance.pk:
                instance.workspace = current_workspace
                instance.user = request.user
            instance.save()
            
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
        recent_chats = ChatMessage.objects.filter(
            session__workspace=current_workspace
        ).order_by('-created_at')[:5]
    else:
        recent_chats = ChatMessage.objects.filter(
            session__user=request.user
        ).order_by('-created_at')[:5] if request.user.is_authenticated else ChatMessage.objects.none()

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
    else:
        gap_analysis = GapAnalysisMetric.objects.filter(
            user=request.user, workspace__isnull=True
        ).order_by('category', 'priority') if request.user.is_authenticated else GapAnalysisMetric.objects.none()
    for metric in gap_analysis:
        calculate_gap_metric_display_properties(metric)

    gap_report_url = '/dashboard/gap-report/'
    
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
        'recent_chats': recent_chats,
        'weekly_activity': weekly_activity,
        'validator_results': validator_results,
        'channel_data': channel_data,
        'gap_analysis': gap_analysis,
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
    if workspace_id:
        if str(session.workspace_id) != str(workspace_id):
            return JsonResponse({"success": False, "error": "That assessment is not in the current workspace."}, status=403)
    elif session.user_id != request.user.id:
        return JsonResponse({"success": False, "error": "You do not have access to that assessment."}, status=403)

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
    workspace_id = request.session.get('current_workspace_id')
    if workspace_id:
        gap_analysis = GapAnalysisMetric.objects.filter(
            models.Q(session__workspace_id=workspace_id) | models.Q(session__isnull=True)
        )
    else:
        gap_analysis = GapAnalysisMetric.objects.filter(
            models.Q(session__user=request.user) | models.Q(session__isnull=True)
        )
    for gap in gap_analysis:
        calculate_gap_metric_display_properties(gap)
            
    return render(request, 'dashboard/partials/gap_analysis_table.html', {'gap_analysis': gap_analysis})

def get_gap_metric_row(request, pk):
    # Only allow access to metrics in user's workspace or user-created metrics
    workspace_id = request.session.get('current_workspace_id')
    if workspace_id:
        metric = get_object_or_404(
            GapAnalysisMetric.objects.filter(
                models.Q(session__workspace_id=workspace_id) | models.Q(session__user=request.user) | models.Q(session__isnull=True)
            ), 
            pk=pk
        )
    else:
        metric = get_object_or_404(
            GapAnalysisMetric.objects.filter(
                models.Q(session__user=request.user) | models.Q(session__isnull=True)
            ), 
            pk=pk
        )
    
    calculate_gap_metric_display_properties(metric)
        
    return render(request, 'dashboard/partials/_gap_analysis_row.html', {'gap': metric})

def refresh_gap_analysis_table(request):
    workspace_id = request.session.get('current_workspace_id')
    if workspace_id:
        gap_analysis = GapAnalysisMetric.objects.filter(
            models.Q(session__workspace_id=workspace_id) | models.Q(session__isnull=True)
        ).order_by('category', 'priority')
    else:
        gap_analysis = GapAnalysisMetric.objects.filter(
            models.Q(session__user=request.user) | models.Q(session__isnull=True)
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
            # Auto-assign workspace and creator for new action items
            if not instance.pk:
                instance.workspace = current_workspace
                instance.created_by = request.user
            instance.save()
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
        instance.delete()
        # Return the updated action items board
        if request.htmx:
            return refresh_action_items(request)
        return redirect('dashboard')
    
    # Show delete confirmation modal
    context = {'instance': instance}
    return render(request, 'dashboard/partials/action_item_confirm_delete.html', context)


@workspace_permission_required('can_assign_tasks', 'session')
def move_action_item(request, pk, new_status):
    if request.method == 'POST':
        # Get current workspace context
        workspace_id = request.session.get('current_workspace_id')
        current_workspace = None
        if workspace_id:
            try:
                from gtm.models_workspace import Workspace
                current_workspace = Workspace.objects.get(id=workspace_id)
            except Workspace.DoesNotExist:
                pass
        
        # Only allow access to action items in user's workspace
        if current_workspace:
            item = get_object_or_404(ActionItem, pk=pk, workspace=current_workspace)
        else:
            item = get_object_or_404(ActionItem, pk=pk, session__user=request.user, workspace__isnull=True)
        
        if new_status in ['todo', 'doing', 'done']:
            item.status = new_status
            item.save()
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
