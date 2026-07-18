from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('notifications/panel/', views.notifications_panel, name='notifications_panel'),
    path('notifications/dropdown/', views.notification_dropdown, name='notification_dropdown'),
    path('notifications/unread-count/', views.notification_unread_count, name='notification_unread_count'),
    path('notifications/<uuid:pk>/read/', views.notification_mark_read, name='notification_mark_read'),
    path('notifications/read-all/', views.notifications_mark_all_read, name='notifications_mark_all_read'),
    path('search/', views.global_search, name='global_search'),
    path('agent/', views.agent_hub, name='agent_hub'),
    path('impact/', views.agent_impact_hub, name='agent_impact_hub'),
    path('agent/chat/', views.dashboard_agent_api, name='dashboard_agent_api'),
    path('agent/clear/', views.dashboard_agent_clear_api, name='dashboard_agent_clear_api'),
    path('agent/context/', views.dashboard_agent_context_api, name='dashboard_agent_context_api'),
    path('agent/insights/refresh/', views.dashboard_agent_insights_refresh, name='dashboard_agent_insights_refresh'),
    path('agent/insights/status/', views.dashboard_agent_insights_status, name='dashboard_agent_insights_status'),
    path('documents/', views.document_list_api, name='document_list_api'),
    path('documents/<uuid:pk>/', views.document_edit_api, name='document_edit_api'),
    path('documents/<uuid:pk>/delete/', views.document_delete_api, name='document_delete_api'),
    path('documents/<uuid:pk>/export/<str:fmt>/', views.document_export, name='document_export'),
    path('agent/team/chat/', views.dashboard_team_agent_api, name='dashboard_team_agent_api'),
    path('agent/<str:agent_type>/chat/', views.dashboard_workspace_agent_api, name='dashboard_workspace_agent_api'),
    path('agent/<str:agent_type>/clear/', views.dashboard_workspace_agent_clear_api, name='dashboard_workspace_agent_clear_api'),
    path('agent/<str:agent_type>/context/', views.dashboard_workspace_agent_context_api, name='dashboard_workspace_agent_context_api'),
    path('tasks/', views.tasks_board, name='tasks_board'),
    path('workspace/hub/', views.workspace_hub, name='workspace_hub'),
    path('kpi/total_sessions/', views.kpi_total_sessions, name='kpi_total_sessions'),
    path('kpi/completed_items/', views.kpi_completed_items, name='kpi_completed_items'),
    path('kpi/pending_items/', views.kpi_pending_items, name='kpi_pending_items'),
    path('insight/<int:pk>/export/<str:fmt>/', views.insight_export, name='insight_export'),
    path('insight/<int:pk>/feedback/', views.insight_feedback, name='insight_feedback'),

    # Gap Analysis Metric CRUD URLs
    path('gap-metric/add/', views.add_edit_gap_metric, name='add_gap_metric'),
    path('gap-metric/<int:pk>/edit/', views.add_edit_gap_metric, name='edit_gap_metric'),
    path('gap-metric/<int:pk>/delete/', views.delete_gap_metric, name='delete_gap_metric'),
    path('gap-metric/<int:pk>/', views.get_gap_metric_row, name='get_gap_metric_row'),
    path('gap-metric/<int:pk>/generate-action-items/', views.generate_action_items_for_gap, name='generate_action_items_for_gap'),
    path('gap-metric/<int:pk>/measure/', views.log_gap_measurement, name='log_gap_measurement'),
    path('gap-metric/<int:pk>/resolve/', views.resolve_gap_metric, name='resolve_gap_metric'),
    path('gap-metric/<int:pk>/reopen/', views.reopen_gap_metric, name='reopen_gap_metric'),
    path('gap-suggestions/refresh/', views.refresh_gap_suggestions, name='refresh_gap_suggestions'),
    path('gap-suggestions/generate/', views.generate_gap_suggestions, name='generate_gap_suggestions'),
    path('gap-suggestions/<int:suggestion_id>/accept/', views.accept_gap_suggestion, name='accept_gap_suggestion'),
    path('gap-suggestions/<int:suggestion_id>/reject/', views.reject_gap_suggestion, name='reject_gap_suggestion'),

    # New URL pattern for gap_analysis_table view
    path('gap-analysis-table/', views.gap_analysis_table, name='gap_analysis_table'),
    path('gap-report/', views.gap_report, name='gap_report'),

    # New URL pattern for refresh_gap_analysis_table view
    path('refresh-gap-analysis-table/', views.refresh_gap_analysis_table, name='refresh_gap_analysis_table'),

    # Action Item URLs
    path('action-item/add/', views.add_edit_action_item, name='add_action_item'),
    path('action-item/<int:pk>/edit/', views.add_edit_action_item, name='edit_action_item'),
    path('action-item/<int:pk>/delete/', views.delete_action_item, name='delete_action_item'),
    path('action-item/<int:pk>/move/<str:new_status>/', views.move_action_item, name='move_action_item'),
    path('action-item/<int:pk>/complete-ai/', views.complete_action_item_ai, name='complete_action_item_ai'),
    path('action-item/<int:pk>/complete-ai-status/', views.complete_action_item_ai_status, name='complete_action_item_ai_status'),
    path('action-item/<int:action_id>/assign/', views.assign_action_item, name='assign_action_item'),
    path('action-item/<int:action_id>/unassign/', views.unassign_action_item, name='unassign_action_item'),
    path('action-item/<int:action_id>/comments/add/', views.add_action_item_comment, name='add_action_item_comment'),
    path('action-item/comments/<uuid:comment_id>/delete/', views.delete_action_item_comment, name='delete_action_item_comment'),
    path('refresh-action-items/', views.refresh_action_items, name='refresh_action_items'),
    
    # Profile URL
    path('profile/', views.profile, name='profile'),

    # Analytics and Settings
    path('analytics/', views.analytics, name='analytics'),
    path('assessment-history/export/<str:fmt>/', views.assessment_history_export, name='assessment_history_export'),
    path('settings/', views.settings_view, name='settings'),
    
    # Workspace management from dashboard
    path('workspace/create/', views.create_workspace_dashboard, name='create_workspace_dashboard'),
    path('workspace/<uuid:workspace_id>/invite/', views.invite_to_workspace, name='invite_to_workspace'),
    path('workspace/<uuid:workspace_id>/edit/', views.edit_workspace, name='edit_workspace'),
    path('workspace/<uuid:workspace_id>/delete/', views.delete_workspace, name='delete_workspace'),
    
    # Resource Library
    path('resources/', views.refresh_resources, name='refresh_resources'),
    path('resources/add/', views.add_edit_resource, name='add_resource'),
    path('resources/<uuid:pk>/edit/', views.add_edit_resource, name='edit_resource'),
    path('resources/<uuid:pk>/delete/', views.delete_resource, name='delete_resource'),

    # Strategic Asset Library
    path('assets/', views.asset_library, name='asset_library'),
    path('assets/<uuid:pk>/audit/', views.trigger_asset_audit, name='trigger_asset_audit'),
    path('assets/<uuid:pk>/audit-result/', views.asset_audit_result, name='asset_audit_result'),
]
