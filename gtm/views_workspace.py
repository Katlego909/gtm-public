from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from .models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation
from .middleware_workspace import workspace_required, role_required


@login_required
def workspace_list(request):
    """List user's workspaces"""
    memberships = WorkspaceMembership.objects.filter(user=request.user).select_related('workspace')
    return render(request, 'gtm/workspace/list.html', {'memberships': memberships})


@login_required
def workspace_create(request):
    """Create new workspace"""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            workspace = Workspace.objects.create(
                name=name,
                created_by=request.user
            )
            WorkspaceMembership.objects.create(
                workspace=workspace,
                user=request.user,
                role='admin'
            )
            messages.success(request, f'Workspace "{name}" created successfully')
            return redirect('workspace:detail', workspace_id=workspace.id)
        messages.error(request, 'Workspace name is required')
    return render(request, 'gtm/workspace/create.html')


@workspace_required
def workspace_detail(request, workspace_id):
    """Workspace detail and management"""
    workspace = request.workspace
    members = WorkspaceMembership.objects.filter(workspace=workspace).select_related('user')
    pending_invites = WorkspaceInvitation.objects.filter(workspace=workspace, accepted_at__isnull=True)
    
    return render(request, 'gtm/workspace/detail.html', {
        'workspace': workspace,
        'members': members,
        'pending_invites': pending_invites,
        'user_membership': request.workspace_membership
    })


@workspace_required
@role_required(['admin', 'manager'])
def workspace_invite(request, workspace_id):
    """Invite users to workspace"""
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        role = request.POST.get('role', 'contributor')
        
        if email:
            invitation = WorkspaceInvitation.objects.create(
                workspace=request.workspace,
                email=email,
                role=role,
                invited_by=request.user
            )
            
            # Send invitation email
            send_mail(
                subject=f'Invitation to {request.workspace.name}',
                message=f'You have been invited to join the workspace "{request.workspace.name}".\n\n'
                       f'Click here to join: {request.build_absolute_uri(f"/workspace/join/{invitation.token}/")}\n\n'
                       f'Invited by: {request.user.get_full_name() or request.user.username}',
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[email]
            )
            
            messages.success(request, f'Invitation sent to {email}')
        else:
            messages.error(request, 'Email is required')
            
    return redirect('workspace:detail', workspace_id=workspace_id)


@login_required
def workspace_join(request, token):
    """Join workspace via invitation"""
    invitation = get_object_or_404(WorkspaceInvitation, token=token, accepted_at__isnull=True)
    
    if invitation.expires_at < timezone.now():
        messages.error(request, 'This invitation has expired')
        return redirect('workspace:list')
    
    if request.method == 'POST':
        # Check if user is already a member
        if not WorkspaceMembership.objects.filter(workspace=invitation.workspace, user=request.user).exists():
            WorkspaceMembership.objects.create(
                workspace=invitation.workspace,
                user=request.user,
                role=invitation.role
            )
            invitation.accepted_at = timezone.now()
            invitation.save()
            messages.success(request, f'Welcome to {invitation.workspace.name}!')
        else:
            messages.info(request, 'You are already a member of this workspace')
        
        return redirect('workspace:detail', workspace_id=invitation.workspace.id)
    
    return render(request, 'gtm/workspace/join.html', {'invitation': invitation})