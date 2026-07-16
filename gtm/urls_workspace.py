from django.urls import path
from . import views_workspace

app_name = 'workspace'

urlpatterns = [
    path('', views_workspace.workspace_list, name='list'),
    path('create/', views_workspace.workspace_create, name='create'),
    path('<uuid:workspace_id>/', views_workspace.workspace_detail, name='detail'),
    path('<uuid:workspace_id>/invite/', views_workspace.workspace_invite, name='invite'),
    path('<uuid:workspace_id>/integrations/', views_workspace.workspace_integrations, name='integrations'),
    path('<uuid:workspace_id>/integrations/hubspot/test/', views_workspace.workspace_integration_test_hubspot, name='integration_test_hubspot'),
    path('<uuid:workspace_id>/integrations/hubspot/disconnect/', views_workspace.workspace_integration_disconnect_hubspot, name='integration_disconnect_hubspot'),
    path('join/<uuid:token>/', views_workspace.workspace_join, name='join'),
]