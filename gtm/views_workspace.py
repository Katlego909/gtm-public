from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.mail import send_mail
from django.conf import settings
from django.utils import timezone
from django.urls import reverse
from .models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation
from .forms import WorkspaceInvitationForm
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
            workspace = Workspace.create_for_user(name=name, user=request.user)
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
    
    # Get the admin user who created this workspace
    admin_member = members.filter(role='admin').first()
    
    invitation_form = WorkspaceInvitationForm()

    return render(request, 'gtm/workspace/detail.html', {
        'workspace': workspace,
        'members': members,
        'pending_invites': pending_invites,
        'user_membership': request.workspace_membership,
        'admin_member': admin_member,
        'invitation_form': invitation_form,
    })


@workspace_required
@role_required(['admin', 'manager'])
def workspace_invite(request, workspace_id):
    """Invite users to workspace"""
    workspace = request.workspace
    dashboard_url = f'/dashboard/?workspace={workspace_id}'
    
    if request.method == 'POST':
        form = WorkspaceInvitationForm(request.POST)
        if form.is_valid():
            email = form.cleaned_data['email']
            
            # Prevent duplicate pending invitations
            if WorkspaceInvitation.objects.filter(workspace=workspace, email__iexact=email, accepted_at__isnull=True).exists():
                messages.warning(request, f'An invitation has already been sent to {email} and is pending.')
                return redirect(dashboard_url)

            # Check if user is already a member
            if WorkspaceMembership.objects.filter(workspace=workspace, user__email__iexact=email).exists():
                messages.warning(request, f'The user with email {email} is already a member of this workspace.')
                return redirect(dashboard_url)

            invitation = form.save(commit=False)
            invitation.workspace = workspace
            invitation.invited_by = request.user
            invitation.save()
            
            # Send invitation email using unified utility
            try:
                from .utils_email import send_workspace_invitation_email
                send_workspace_invitation_email(invitation, request)
                messages.success(request, f'Invitation sent successfully to {email}!')
            except Exception as e:
                messages.error(request, f'Failed to send invitation email: {e}')
                invitation.delete()

        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.capitalize()}: {error}")
            
    return redirect(dashboard_url)


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