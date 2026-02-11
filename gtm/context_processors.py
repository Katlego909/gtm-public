"""
Context processor for workspace permissions.
Makes workspace permissions available in all templates.
"""

def workspace_permissions(request):
    """
    Add workspace permission context to all templates.
    """
    context = {
        'user_can_invite': False,
        'user_can_assign_tasks': False,
        'user_is_admin': False,
        'user_is_workspace_member': False,
        'current_workspace': None,
        'user_membership': None,
    }
    
    if not request.user.is_authenticated:
        return context
    
    # Get current workspace from session
    workspace_id = request.session.get('current_workspace_id')
    if not workspace_id:
        return context
    
    try:
        from gtm.models_workspace import Workspace, WorkspaceMembership
        
        # Get workspace and user membership
        workspace = Workspace.objects.get(id=workspace_id)
        membership = WorkspaceMembership.objects.get(
            user=request.user,
            workspace=workspace,
            is_active=True
        )
        
        # Update context with workspace info
        context.update({
            'current_workspace': workspace,
            'user_membership': membership,
            'user_is_workspace_member': True,
            'user_can_invite': membership.can_invite_users,
            'user_can_assign_tasks': membership.can_assign_tasks,
            'user_is_admin': membership.role in ['admin', 'funti3r_consultant'],
            'user_is_viewer_only': membership.role == 'viewer',
        })
        
    except (Workspace.DoesNotExist, WorkspaceMembership.DoesNotExist):
        pass
    
    return context