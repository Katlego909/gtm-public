from django.urls import path
from . import views_workspace

app_name = 'workspace'

urlpatterns = [
    path('', views_workspace.workspace_list, name='list'),
    path('create/', views_workspace.workspace_create, name='create'),
    path('<uuid:workspace_id>/', views_workspace.workspace_detail, name='detail'),
    path('<uuid:workspace_id>/invite/', views_workspace.workspace_invite, name='invite'),
    path('join/<uuid:token>/', views_workspace.workspace_join, name='join'),
]