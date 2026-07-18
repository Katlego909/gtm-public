import datetime
import markdown
from django.utils import timezone
from django.urls import reverse
from gtm.models import AssessmentSession, ActionItem, ResultSnapshot, ToolRecommendation, ChatMessage
from dashboard.models import Channel, ChannelAnalytics, GapAnalysisMetric, GapAnalysisSuggestion
from gtm.services import _compute_scores, _band_for_score
from gtm.ai_chat import get_suggested_prompts

def _md(text):
    return markdown.markdown(text)


def _build_assessment_history_export_rows(sessions_qs):
    """Build full (unbounded) assessment-history rows for CSV export.

    Prefers the cached ResultSnapshot over recomputing scores -- mirrors the
    approach gtm/views/profile.py's `history` view already uses -- and only
    falls back to _compute_scores for completed sessions that were never
    viewed on the results page yet (so no snapshot exists).
    """
    rows = []
    for session in sessions_qs:
        snap = getattr(session, 'snapshot', None)
        if snap:
            overall = snap.overall
            band_stage = snap.band_stage or 'Unknown'
        else:
            _, overall = _compute_scores(session)
            band = _band_for_score(overall)
            band_stage = band.stage if band else 'Unknown'
        rows.append({
            'company_name': session.company_name or 'Unnamed',
            'industry': session.industry or 'N/A',
            'overall_score': round(overall, 1),
            'band_stage': band_stage,
            'created_at': session.created_at,
        })
    return rows

def get_dashboard_context(request, current_workspace, user_workspaces, agent_session_id):
    from dashboard.views import _load_pending_gap_suggestions
    from dashboard.utils import calculate_gap_metric_display_properties
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
    
    # Calculate Action Item Velocity (Average Time to Completion)
    completed_items_qs = action_items_qs.filter(status='done', completed_at__isnull=False)
    velocity_days = None
    if completed_items_qs.exists():
        total_seconds = sum((item.completed_at - item.created_at).total_seconds() for item in completed_items_qs)
        avg_seconds = total_seconds / completed_items_qs.count()
        velocity_days = round(avg_seconds / 86400, 1)
        

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

    # Tool recommendations: matched to this workspace's weakest-scoring
    # categories from its latest completed assessment, mirroring the
    # weak-category matching already used on the Results page
    # (gtm/views/results.py) rather than showing arbitrary rows.
    top_tool_groups = []
    latest_snapshot = ResultSnapshot.objects.filter(
        session__in=assessments_qs, session__is_completed=True
    ).order_by('-created_at').first()

    if latest_snapshot and latest_snapshot.category_breakdown:
        # Only genuinely weak categories (avg < 75, the same "Improving"
        # threshold used for the severity badge below) -- ranking bottom-N
        # regardless of value would surface a category as "recommended" even
        # when everything is already scoring well.
        categories_sorted = sorted(latest_snapshot.category_breakdown, key=lambda c: c.get('avg', 0))
        weak_entries = [c for c in categories_sorted if (c.get('avg', 0) / 5 * 100) < 75][:3]
        weak_names = [c.get('category') for c in weak_entries if c.get('category')]

        tools_by_category_name = {}
        for tool in ToolRecommendation.objects.filter(category__name__in=weak_names).select_related('category'):
            tool.tools_list = [t.strip().lower().title() for t in tool.tools.split(',')] if tool.tools else []
            tools_by_category_name.setdefault(tool.category.name, []).append(tool)

        for entry in weak_entries:
            name = entry.get('category')
            tools = tools_by_category_name.get(name)
            if not tools:
                continue
            avg = entry.get('avg', 0)
            avg_pct = avg / 5 * 100
            if avg_pct < 50:
                severity_class, severity_label = 'bg-red-100 text-red-500', 'Needs Attention'
            elif avg_pct < 75:
                severity_class, severity_label = 'bg-yellow-100 text-yellow-700', 'Improving'
            else:
                severity_class, severity_label = 'bg-green-100 text-green-700', 'Strong'
            top_tool_groups.append({
                'category_name': name,
                'score': round(avg, 1),
                'severity_class': severity_class,
                'severity_label': severity_label,
                'tools': tools,
            })

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
                chats = list(reversed(history_qs))
                for chat in chats:
                    chat.response_html = _md(chat.response or '')
                dashboard_agent_history = chats

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
    gap_analysis = GapAnalysisMetric.objects.none()
    latest_completed_gap_session = None

    if current_workspace:
        gap_analysis = GapAnalysisMetric.objects.filter(
            workspace=current_workspace
        ).order_by('category', 'priority')
        latest_completed_gap_session = AssessmentSession.objects.filter(
            workspace=current_workspace,
            is_completed=True,
        ).order_by('-created_at').first()
    elif request.user.is_authenticated:
        gap_analysis = GapAnalysisMetric.objects.filter(
            user=request.user, workspace__isnull=True
        ).order_by('category', 'priority')
        latest_completed_gap_session = AssessmentSession.objects.filter(
            user=request.user,
            is_completed=True,
        ).order_by('-created_at').first()

    # Risk Classification & Resource Recommendations from the latest session
    latest_score_risk = latest_completed_gap_session.snapshot.ai_risk_status if latest_completed_gap_session and hasattr(latest_completed_gap_session, 'snapshot') else None
    
    ai_resource_recommendations = []
    if latest_completed_gap_session:
        ai_resource_recommendations = list(latest_completed_gap_session.ai_resource_matches.all().select_related('resource'))

    # Deferred import: dashboard.views.helpers imports this module at load time.
    from dashboard.views.helpers import prepare_gap_metrics_for_display
    gap_analysis = prepare_gap_metrics_for_display(
        gap_analysis,
        current_workspace,
        request.user if request.user.is_authenticated else None,
    )

    gap_suggestions = _load_pending_gap_suggestions(request, current_workspace) if request.user.is_authenticated else []

    if current_workspace:
        gap_report_url = f"{reverse('gap_report')}?workspace={current_workspace.id}"
        assessment_history_export_url = f"{reverse('assessment_history_export', args=['csv'])}?workspace={current_workspace.id}"
    else:
        gap_report_url = reverse('gap_report')
        assessment_history_export_url = reverse('assessment_history_export', args=['csv'])

    context = {
        'total_sessions': total_sessions,
        'completed_items': completed_items,
        'pending_items': pending_items,
        'velocity_days': velocity_days,
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
        'top_tool_groups': top_tool_groups,
        'insights': insights,
        'weekly_activity': weekly_activity,
        'items_created_per_day': [d['items_created'] for d in weekly_activity],
        'items_completed_per_day': [d['items_completed'] for d in weekly_activity],
        'validator_results': validator_results,
        'channel_data': channel_data,
        'gap_analysis': gap_analysis,
        'gap_suggestions': gap_suggestions,
        'latest_completed_gap_session': latest_completed_gap_session,
        'latest_score_risk': latest_score_risk,
        'ai_resource_recommendations': ai_resource_recommendations,
        'gap_report_url': gap_report_url,
        'assessment_history_export_url': assessment_history_export_url,
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
    return context
