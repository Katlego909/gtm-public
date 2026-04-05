"""
Permission decorators for workspace role-based access control.
"""

from functools import wraps
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from .models_workspace import Workspace, WorkspaceMembership


def workspace_permission_required(permission_name, workspace_param='workspace_id'):
    """
    Decorator to check workspace permissions.
    
    Args:
        permission_name: Method name on WorkspaceMembership to check (e.g. 'can_assign_tasks')
        workspace_param: Parameter name or 'session' to get workspace from session
    """
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped_view(request, *args, **kwargs):
            # Get workspace ID
            if workspace_param == 'session':
                workspace_id = request.session.get('current_workspace_id')
            else:
                workspace_id = kwargs.get(workspace_param) or request.GET.get('workspace')
            
            if not workspace_id:
                return HttpResponseForbidden("No workspace specified")
            
            # Get workspace and user membership
            try:
                workspace = Workspace.objects.get(id=workspace_id)
                membership = WorkspaceMembership.objects.get(
                    user=request.user,
                    workspace=workspace,
                    is_active=True
                )
            except (Workspace.DoesNotExist, WorkspaceMembership.DoesNotExist):
                if request.headers.get('HX-Request'):
                    from django.shortcuts import render
                    return render(request, 'dashboard/partials/error_modal.html', {
                        'title': 'Access Denied',
                        'message': 'You are not a member of this workspace or your access has been revoked.'
                    })
                return HttpResponseForbidden("Access denied to this workspace")
            
            # Check specific permission
            if not hasattr(membership, permission_name):
                return HttpResponseForbidden("Invalid permission check")
            
            if not getattr(membership, permission_name):
                if request.headers.get('HX-Request'):
                    from django.shortcuts import render
                    return render(request, 'dashboard/partials/error_modal.html', {
                        'title': 'Permission Required',
                        'message': f'You need {permission_name.replace("_", " ")} permission to perform this action.'
                    })
                return JsonResponse({'error': 'Permission denied'}, status=403)
                return HttpResponseForbidden("You don't have permission for this action")
            
            # Add workspace context to request
            request.workspace = workspace
            request.membership = membership
            
            return view_func(request, *args, **kwargs)
        
        return _wrapped_view
    return decorator


def workspace_admin_required(workspace_param='workspace_id'):
    """Shortcut decorator for admin-only actions."""
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped_view(request, *args, **kwargs):
            # Get workspace ID
            if workspace_param == 'session':
                workspace_id = request.session.get('current_workspace_id')
            else:
                workspace_id = kwargs.get(workspace_param) or request.GET.get('workspace')
            
            if not workspace_id:
                return HttpResponseForbidden("No workspace specified")
            
            # Check admin access
            try:
                workspace = Workspace.objects.get(id=workspace_id)
                membership = WorkspaceMembership.objects.get(
                    user=request.user,
                    workspace=workspace,
                    is_active=True,
                    role__in=['admin', 'funti3r_consultant']
                )
            except (Workspace.DoesNotExist, WorkspaceMembership.DoesNotExist):
                if request.headers.get('HX-Request'):
                    return JsonResponse({'error': 'Admin access required'}, status=403)
                return HttpResponseForbidden("Admin access required")
            
            # Add context to request
            request.workspace = workspace
            request.membership = membership
            
            return view_func(request, *args, **kwargs)
        
        return _wrapped_view
    return decorator


def workspace_member_required(workspace_param='workspace_id'):
    """Basic workspace membership check."""
    def decorator(view_func):
        @wraps(view_func)
        @login_required
        def _wrapped_view(request, *args, **kwargs):
            # Get workspace ID
            if workspace_param == 'session':
                workspace_id = request.session.get('current_workspace_id')
            else:
                workspace_id = kwargs.get(workspace_param) or request.GET.get('workspace')
            
            if not workspace_id:
                return HttpResponseForbidden("No workspace specified")
            
            # Check membership
            try:
                workspace = Workspace.objects.get(id=workspace_id)
                membership = WorkspaceMembership.objects.get(
                    user=request.user,
                    workspace=workspace,
                    is_active=True
                )
            except (Workspace.DoesNotExist, WorkspaceMembership.DoesNotExist):
                if request.headers.get('HX-Request'):
                    from django.shortcuts import render
                    return render(request, 'dashboard/partials/error_modal.html', {
                        'title': 'Access Denied',
                        'message': 'You are not a member of this workspace or your access has been revoked.'
                    })
                return HttpResponseForbidden("Access denied to this workspace")
            
            # Add context to request
            request.workspace = workspace
            request.membership = membership
            
            return view_func(request, *args, **kwargs)
        
        return _wrapped_view
    return decorator