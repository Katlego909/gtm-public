# gtm/middleware_workspace.py
"""
Workspace authorization middleware.
Handles workspace context and role-based access control.
Keeps authorization logic separate from django-allauth authentication.
"""

from django.shortcuts import redirect, get_object_or_404
from django.urls import reverse
from django.contrib import messages
from django.http import Http404
from .models_workspace import Workspace, WorkspaceMembership
from uuid import UUID


class WorkspaceMiddleware:
    """
    Adds workspace context to requests and handles role-based authorization.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Add workspace context to request
        response = self.process_request(request)
        if response:
            return response
        
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
        workspace_identifier = None

        def _is_uuid(value):
            try:
                UUID(str(value))
                return True
            except (TypeError, ValueError):
                return False
        
        # Extract workspace identifier from /workspace/{id-or-slug}/...
        path_parts = request.path.strip('/').split('/')
        if len(path_parts) >= 2 and path_parts[0] == 'workspace':
            route_segment = path_parts[1]

            # Route segments that are not workspace identifiers.
            if route_segment in {'create', 'join'}:
                request.workspace = None
                request.workspace_membership = None
                return

            workspace_identifier = route_segment
        
        # Get workspace from URL identifier or user's default workspace
        if workspace_identifier:
            try:
                if _is_uuid(workspace_identifier):
                    workspace = get_object_or_404(Workspace, id=workspace_identifier, is_active=True)
                else:
                    workspace = get_object_or_404(Workspace, slug=workspace_identifier, is_active=True)
                # NOTE: do NOT persist current_workspace_id here — membership is not
                # verified yet. Storing it before the check below would poison the
                # session with a workspace the user cannot access, causing an
                # infinite redirect loop on every subsequent workspace request.
            except Http404:
                # Invalid workspace, redirect to workspace selection
                return redirect('workspace_hub')
        else:
            # Try to get workspace from session. Fast path (every request,
            # including polls): one joined query that confirms the workspace is
            # active AND the user is a member, instead of fetching the workspace
            # and then the membership separately.
            workspace_id = request.session.get('current_workspace_id')
            if workspace_id:
                membership = WorkspaceMembership.objects.select_related('workspace').filter(
                    user=request.user,
                    workspace_id=workspace_id,
                    workspace__is_active=True,
                    is_active=True,
                ).first()
                if membership is not None:
                    request.workspace = membership.workspace
                    request.workspace_membership = membership
                    return
                # Slow path: work out why (deleted workspace vs. lost access).
                try:
                    workspace = Workspace.objects.get(id=workspace_id, is_active=True)
                except Workspace.DoesNotExist:
                    # Workspace deleted, clear session
                    del request.session['current_workspace_id']

            # If no workspace in session, get user's default (membership and
            # workspace in one query) and remember it.
            if not workspace:
                membership = WorkspaceMembership.objects.select_related('workspace').filter(
                    user=request.user,
                    is_active=True
                ).first()
                if membership:
                    request.workspace = membership.workspace
                    request.workspace_membership = membership
                    request.session['current_workspace_id'] = str(membership.workspace_id)
                    return
        
        # Add workspace context to request
        request.workspace = workspace
        request.workspace_membership = None
        
        if workspace:
            try:
                request.workspace_membership = WorkspaceMembership.objects.select_related('workspace').get(
                    user=request.user,
                    workspace=workspace,
                    is_active=True
                )
                # Membership confirmed — now it is safe to remember this workspace
                # for subsequent requests that don't carry an identifier in the URL.
                # Only write when it changes: with signed-cookie sessions every
                # write re-signs and re-sends the cookie.
                if request.session.get('current_workspace_id') != str(workspace.id):
                    request.session['current_workspace_id'] = str(workspace.id)
            except WorkspaceMembership.DoesNotExist:
                # User not member of this workspace. Clear any stale session pointer
                # so we don't redirect-loop on the next identifier-less request.
                if request.session.get('current_workspace_id') == str(workspace.id):
                    del request.session['current_workspace_id']
                messages.error(request, "You don't have access to this workspace.")
                return redirect('workspace_hub')


def workspace_required(view_func):
    """
    Decorator to require workspace context for views.
    """
    def wrapper(request, *args, **kwargs):
        if not hasattr(request, 'workspace') or not request.workspace:
            messages.error(request, "Please select a workspace to continue.")
            return redirect('workspace_hub')
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
                return redirect('workspace_hub')
                
            if request.workspace_membership.role not in allowed_roles:
                messages.error(request, "You don't have sufficient permissions for this action.")
                return redirect('/dashboard/')
                
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