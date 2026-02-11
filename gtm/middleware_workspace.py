# gtm/middleware_workspace.py
"""
Workspace authorization middleware.
Handles workspace context and role-based access control.
Keeps authorization logic separate from django-allauth authentication.
"""

from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from .models_workspace import Workspace, WorkspaceMembership


class WorkspaceMiddleware:
    """
    Adds workspace context to requests and handles role-based authorization.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Add workspace context to request
        self.process_request(request)
        
        response = self.get_response(request)
        return response

    def process_request(self, request):
        """Add workspace context to request"""
        
        # Skip for unauthenticated users and auth URLs
        if not request.user.is_authenticated:
            return
            
        # Skip for allauth URLs to avoid conflicts
        if request.path.startswith('/accounts/'):
            return
            
        # Try to get workspace from URL or session
        workspace = None
        workspace_slug = None
        
        # Extract workspace slug from URL patterns like /workspace/{slug}/...
        path_parts = request.path.strip('/').split('/')
        if len(path_parts) >= 2 and path_parts[0] == 'workspace':
            workspace_slug = path_parts[1]
        
        # Get workspace from slug or get user's default workspace
        if workspace_slug:
            try:
                workspace = get_object_or_404(Workspace, slug=workspace_slug, is_active=True)
                # Store in session for subsequent requests
                request.session['current_workspace_id'] = str(workspace.id)
            except:
                # Invalid workspace, redirect to workspace selection
                return redirect('workspace_list')
        else:
            # Try to get workspace from session
            workspace_id = request.session.get('current_workspace_id')
            if workspace_id:
                try:
                    workspace = Workspace.objects.get(id=workspace_id, is_active=True)
                except Workspace.DoesNotExist:
                    # Workspace deleted, clear session
                    del request.session['current_workspace_id']
            
            # If no workspace in session, get user's default
            if not workspace:
                membership = WorkspaceMembership.objects.filter(
                    user=request.user, 
                    is_active=True
                ).first()
                if membership:
                    workspace = membership.workspace
                    request.session['current_workspace_id'] = str(workspace.id)
        
        # Add workspace context to request
        request.workspace = workspace
        request.workspace_membership = None
        
        if workspace:
            try:
                request.workspace_membership = WorkspaceMembership.objects.get(
                    user=request.user,
                    workspace=workspace,
                    is_active=True
                )
            except WorkspaceMembership.DoesNotExist:
                # User not member of this workspace
                messages.error(request, "You don't have access to this workspace.")
                return redirect('workspace_list')


def workspace_required(view_func):
    """
    Decorator to require workspace context for views.
    """
    def wrapper(request, *args, **kwargs):
        if not hasattr(request, 'workspace') or not request.workspace:
            messages.error(request, "Please select a workspace to continue.")
            return redirect('workspace_list')
        return view_func(request, *args, **kwargs)
    return wrapper


def role_required(allowed_roles):
    """
    Decorator to require specific roles for views.
    Usage: @role_required(['admin', 'manager'])
    """
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            if not hasattr(request, 'workspace_membership') or not request.workspace_membership:
                messages.error(request, "Workspace access required.")
                return redirect('workspace_list')
                
            if request.workspace_membership.role not in allowed_roles:
                messages.error(request, "You don't have sufficient permissions for this action.")
                return redirect('dashboard')
                
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator


def funti3r_required(view_func):
    """
    Decorator to require Funti3r consultant role.
    """
    return role_required(['funti3r_consultant'])(view_func)


def admin_or_manager_required(view_func):
    """
    Decorator to require admin or manager role.
    """
    return role_required(['admin', 'manager', 'funti3r_consultant'])(view_func)