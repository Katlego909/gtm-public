from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('kpi/total_sessions/', views.kpi_total_sessions, name='kpi_total_sessions'),
    path('kpi/completed_items/', views.kpi_completed_items, name='kpi_completed_items'),
    path('kpi/pending_items/', views.kpi_pending_items, name='kpi_pending_items'),
    path('insight/<int:pk>/export/', views.insight_export, name='insight_export'),
    path('insight/<int:pk>/feedback/', views.insight_feedback, name='insight_feedback'),

    # Gap Analysis Metric CRUD URLs
    path('gap-metric/add/', views.add_edit_gap_metric, name='add_gap_metric'),
    path('gap-metric/<int:pk>/edit/', views.add_edit_gap_metric, name='edit_gap_metric'),
    path('gap-metric/<int:pk>/delete/', views.delete_gap_metric, name='delete_gap_metric'),
    path('gap-metric/<int:pk>/', views.get_gap_metric_row, name='get_gap_metric_row'),

    # New URL pattern for gap_analysis_table view
    path('gap-analysis-table/', views.gap_analysis_table, name='gap_analysis_table'),

    # New URL pattern for refresh_gap_analysis_table view
    path('refresh-gap-analysis-table/', views.refresh_gap_analysis_table, name='refresh_gap_analysis_table'),

    # Action Item URLs
    path('action-item/add/', views.add_edit_action_item, name='add_action_item'),
    path('action-item/<int:pk>/edit/', views.add_edit_action_item, name='edit_action_item'),
    path('action-item/<int:pk>/delete/', views.delete_action_item, name='delete_action_item'),
    path('action-item/<int:pk>/move/<str:new_status>/', views.move_action_item, name='move_action_item'),
    path('action-item/<int:action_id>/assign/', views.assign_action_item, name='assign_action_item'),
    path('action-item/<int:action_id>/unassign/', views.unassign_action_item, name='unassign_action_item'),
    path('refresh-action-items/', views.refresh_action_items, name='refresh_action_items'),
    
    # Profile URL
    path('profile/', views.profile, name='profile'),
    
    # Workspace management from dashboard
    path('workspace/create/', views.create_workspace_dashboard, name='create_workspace_dashboard'),
    path('workspace/<uuid:workspace_id>/invite/', views.invite_to_workspace_dashboard, name='invite_to_workspace_dashboard'),
]
