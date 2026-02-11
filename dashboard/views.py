# dashboard/views.py
"""
Dashboard views for GTM Validator
"""

import datetime
import json
import markdown

from django.shortcuts import get_object_or_404, render, redirect
from django.http import HttpResponse, JsonResponse, Http404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.vary import vary_on_headers
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User

from gtm.models import (
    AssessmentSession,
    ActionItem,
    ToolRecommendation,
    ResultSnapshot,
    ChatMessage,
)
from gtm.models_workspace import Workspace, WorkspaceMembership
from dashboard.models import Channel, ChannelAnalytics, GapAnalysisMetric
from .forms import GapAnalysisMetricForm, ActionItemForm, UserProfileForm
from gtm.views import _compute_scores, _band_for_score

from django.urls import reverse
from .utils import calculate_gap_metric_display_properties

# ================================================================
# GAP ANALYSIS METRIC CRUD VIEWS
# ================================================================

@vary_on_headers('HX-Request')
def add_edit_gap_metric(request, pk=None):
    if pk:
        instance = get_object_or_404(GapAnalysisMetric, pk=pk)
        title = "Edit Gap Metric"
    else:
        instance = None
        title = "Add Gap Metric"

    if request.method == 'POST':
        form = GapAnalysisMetricForm(request.POST, instance=instance)
        if form.is_valid():
            instance = form.save(commit=False)
            if not hasattr(instance, 'user') or not instance.user:
                instance.user = request.user
            instance.save()
            
            if request.htmx:
                # Return the updated gap analysis table and close modal
                gap_analysis = GapAnalysisMetric.objects.all().order_by('category', 'priority')
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


@vary_on_headers('HX-Request')
def delete_gap_metric(request, pk):
    try:
        instance = get_object_or_404(GapAnalysisMetric, pk=pk)
    except Http404:
        # If the object doesn't exist, and it's an HTMX request, 
        # redirect back to dashboard
        if request.htmx:
            response = HttpResponse(status=204)
            response['HX-Redirect'] = reverse('dashboard')
            return response
        raise

    if request.method == 'POST':
        instance.delete()
        if request.htmx:
            # Return empty response - row will be removed via hx-swap="outerHTML swap:0.3s"
            return HttpResponse('')
        return redirect('dashboard')

    context = {'instance': instance}

    if request.htmx:
        return render(request, 'dashboard/partials/_gap_metric_confirm_delete_inline.html', context)
    
    return render(request, 'dashboard/gap_metric_confirm_delete.html', context)
# ================================================================
# INSIGHT VIEWS
# ================================================================

@csrf_exempt
def insight_export(request, pk):
    """Export AI insight/playbook as a text file."""
    insight = get_object_or_404(ResultSnapshot, pk=pk)
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
    total_sessions = AssessmentSession.objects.count()
    html = f'<div id="total-sessions">{total_sessions}</div>'
    return HttpResponse(html)


def kpi_completed_items(request):
    """Return completed action items count as an HTML fragment."""
    completed_items = ActionItem.objects.filter(status='done').count()
    html = f'<div id="completed-items">{completed_items}</div>'
    return HttpResponse(html)


def kpi_pending_items(request):
    """Return pending action items count as an HTML fragment."""
    pending_items = ActionItem.objects.exclude(status='done').count()
    html = f'<div id="pending-items">{pending_items}</div>'
    return HttpResponse(html)


# ================================================================
# DASHBOARD VIEW
# ================================================================

@vary_on_headers('HX-Request')
@login_required
def dashboard(request):
    """Main dashboard view - now workspace-aware for team collaboration."""
    
    # Get workspace context
    workspace_id = request.GET.get('workspace')
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
    
    # Filter data by workspace if selected
    if current_workspace:
        # Workspace-scoped data
        assessments_qs = AssessmentSession.objects.filter(workspace=current_workspace)
        action_items_qs = ActionItem.objects.filter(workspace=current_workspace)
    else:
        # Fallback to user's data (legacy support)
        assessments_qs = AssessmentSession.objects.filter(user=request.user) if request.user.is_authenticated else AssessmentSession.objects.none()
        action_items_qs = ActionItem.objects.filter(session__user=request.user) if request.user.is_authenticated else ActionItem.objects.none()

    # Recent validator results for dashboard
    if current_workspace:
        validator_results = ResultSnapshot.objects.filter(session__workspace=current_workspace).order_by('-created_at')[:10]
    else:
        validator_results = ResultSnapshot.objects.filter(session__user=request.user).order_by('-created_at')[:10] if request.user.is_authenticated else []

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
    this_week_completed = action_items_qs.filter(
        status='done', 
        created_at__date__gte=this_week_start
    ).count()
    last_week_completed = action_items_qs.filter(
        status='done',
        created_at__date__gte=last_week_start,
        created_at__date__lte=last_week_end
    ).count()
    
    # Calculate percentage change for completed items
    completed_change = 0
    completed_change_positive = True
    completed_has_meaningful_change = False
    if last_week_completed > 0:
        completed_change = round(((this_week_completed - last_week_completed) / last_week_completed) * 100)
        completed_change_positive = completed_change >= 0
        completed_has_meaningful_change = completed_change != 0
    # Don't show percentage for 0 -> anything transitions
        
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

    # Insights (from ResultSnapshot.ai_playbook)
    insights_qs = ResultSnapshot.objects.exclude(ai_playbook="").order_by('-created_at')[:5]
    insights = []
    for insight in insights_qs:
        insight.playbook_html = markdown.markdown(insight.ai_playbook or "")
        insights.append(insight)

    # Recent chat messages
    recent_chats = ChatMessage.objects.order_by('-created_at')[:5]

    # GTM Assessment History & Trends (workspace-scoped)
    assessment_history = []
    assessment_stats = None
    score_trend_data = {'labels': [], 'scores': []}
    
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

    # Convert markdown notes to HTML for all relevant items
    def convert_notes(items):
        for item in items:
            item.note_html = markdown.markdown(item.note or "")
        return items

    recent_items = convert_notes(list(recent_items))
    top_todo = convert_notes(list(top_todo))
    top_doing = convert_notes(list(top_doing))
    top_done = convert_notes(list(top_done))

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

    # --- Gap Analysis Logic ---
    gap_analysis = GapAnalysisMetric.objects.all().order_by('category', 'priority')
    for metric in gap_analysis:
        calculate_gap_metric_display_properties(metric)

    gap_report_url = '/dashboard/gap-report/'
    
    # Get team members for the current workspace
    team_members = []
    if current_workspace:
        memberships = WorkspaceMembership.objects.filter(workspace=current_workspace).select_related('user')
        team_members = [{'membership': m, 'user': m.user} for m in memberships]

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
        'top_todo': top_todo,
        'top_doing': top_doing,
        'top_done': top_done,
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
        # Workspace context
        'current_workspace': current_workspace,
        'user_workspaces': user_workspaces,
        'team_members': team_members,
    }

    if request.htmx:
        return render(request, 'dashboard/partials/dashboard_content.html', context)

    return render(request, 'dashboard/home.html', context)

def gap_analysis_table(request):
    gap_analysis = GapAnalysisMetric.objects.all()
    for gap in gap_analysis:
        calculate_gap_metric_display_properties(gap)
            
    return render(request, 'dashboard/partials/gap_analysis_table.html', {'gap_analysis': gap_analysis})

def get_gap_metric_row(request, pk):
    metric = get_object_or_404(GapAnalysisMetric, pk=pk)
    
    calculate_gap_metric_display_properties(metric)
        
    return render(request, 'dashboard/partials/_gap_analysis_row.html', {'gap': metric})

def refresh_gap_analysis_table(request):
    # Use .all() to match the main dashboard view logic
    gap_analysis = GapAnalysisMetric.objects.all().order_by('category', 'priority')
    
    for metric in gap_analysis:
        calculate_gap_metric_display_properties(metric)
            
    return render(request, 'dashboard/partials/_gap_analysis_table_rows.html', {'gap_analysis': gap_analysis})


# ================================================================
# ACTION ITEM VIEWS
# ================================================================

def refresh_action_items(request):
    """Returns the updated action items board - workspace-aware."""
    
    # Get workspace context from request
    workspace_id = request.GET.get('workspace')
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
        action_items_qs = ActionItem.objects.filter(session__user=request.user) if request.user.is_authenticated else ActionItem.objects.none()
    
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


@vary_on_headers('HX-Request')
def add_edit_action_item(request, pk=None):
    if pk:
        instance = get_object_or_404(ActionItem, pk=pk)
        title = "Edit Action Item"
    else:
        instance = None
        title = "Add Action Item"

    if request.method == 'POST':
        form = ActionItemForm(request.POST, instance=instance)
        if form.is_valid():
            instance = form.save()
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
        form = ActionItemForm(instance=instance)

    context = {
        'form': form,
        'title': title,
        'instance': instance
    }
    
    return render(request, 'dashboard/partials/action_item_form.html', context)


@vary_on_headers('HX-Request')
def delete_action_item(request, pk):
    instance = get_object_or_404(ActionItem, pk=pk)
    if request.method == 'POST':
        instance.delete()
        # Return the updated action items board
        if request.htmx:
            return refresh_action_items(request)
        return redirect('dashboard')
    
    return render(request, 'dashboard/partials/action_item_confirm_delete.html', {'instance': instance})


@csrf_exempt
def move_action_item(request, pk, new_status):
    if request.method == 'POST':
        item = get_object_or_404(ActionItem, pk=pk)
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

@login_required
def assign_action_item(request, action_id):
    """Assign an action item to a team member."""
    action_item = get_object_or_404(ActionItem, id=action_id)
    
    # Check user has access to this action item's workspace
    if action_item.workspace:
        membership = WorkspaceMembership.objects.filter(
            workspace=action_item.workspace, 
            user=request.user
        ).first()
        if not membership or membership.role not in ['admin', 'manager', 'funti3r_consultant']:
            return JsonResponse({'error': 'No permission'}, status=403)
    
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


@login_required
def unassign_action_item(request, action_id):
    """Remove assignment from an action item."""
    action_item = get_object_or_404(ActionItem, id=action_id)
    
    # Check user has access
    if action_item.workspace:
        membership = WorkspaceMembership.objects.filter(
            workspace=action_item.workspace, 
            user=request.user
        ).first()
        if not membership or membership.role not in ['admin', 'manager', 'funti3r_consultant']:
            return JsonResponse({'error': 'No permission'}, status=403)
    
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
            from gtm.models_workspace import Workspace, WorkspaceMembership
            from django.utils.text import slugify
            import uuid
            
            # Generate unique slug
            slug = slugify(name)
            counter = 1
            original_slug = slug
            while Workspace.objects.filter(slug=slug).exists():
                slug = f"{original_slug}-{counter}"
                counter += 1
            
            workspace = Workspace.objects.create(
                name=name,
                slug=slug
            )
            WorkspaceMembership.objects.create(
                workspace=workspace,
                user=request.user,
                role='admin'
            )
            # Redirect to dashboard with new workspace selected
            return redirect(f'/dashboard/?workspace={workspace.id}')
        
    return redirect('/dashboard/')


@login_required 
def invite_to_workspace_dashboard(request, workspace_id):
    """Invite user to workspace from dashboard and return updated dashboard view."""
    from gtm.models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation
    
    workspace = get_object_or_404(Workspace, id=workspace_id)
    
    # Check user has permission
    membership = WorkspaceMembership.objects.filter(
        workspace=workspace, 
        user=request.user
    ).first()
    if not membership or membership.role not in ['admin', 'manager']:
        return JsonResponse({'error': 'No permission'}, status=403)
    
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        role = request.POST.get('role', 'contributor')
        
        if email:
            from django.core.mail import send_mail
            from django.conf import settings
            
            invitation = WorkspaceInvitation.objects.create(
                workspace=workspace,
                email=email,
                role=role,
                invited_by=request.user
            )
            
            # Send invitation email
            try:
                send_mail(
                    subject=f'Invitation to {workspace.name}',
                    message=f'You have been invited to join the workspace "{workspace.name}".\n\n'
                           f'Click here to join: {request.build_absolute_uri(f"/workspace/join/{invitation.token}/")}\n\n'
                           f'Invited by: {request.user.get_full_name() or request.user.username}',
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[email]
                )
            except Exception:
                pass  # Silently handle email errors for now
            
            # Redirect to dashboard with current workspace
            return redirect(f'/dashboard/?workspace={workspace_id}')
        
    return redirect(f'/dashboard/?workspace={workspace_id}')
