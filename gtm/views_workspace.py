from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.utils import timezone
from django.urls import reverse
from .models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation
from .forms import WorkspaceInvitationForm
from .middleware_workspace import workspace_required, admin_or_manager_required
from .decorators import workspace_permission_required


def _hub_redirect(workspace_id=None):
    """Workspace management lives in the dashboard's Workspace Hub; the legacy
    gtm/workspace pages redirect there."""
    url = reverse('workspace_hub')
    if workspace_id:
        url = f'{url}?workspace={workspace_id}'
    return redirect(url)


@login_required
def workspace_list(request):
    return _hub_redirect()


@login_required
def workspace_create(request):
    """Create new workspace"""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            if Workspace.user_at_creation_cap(request.user):
                messages.error(
                    request,
                    "You've reached the workspace limit for the beta. "
                    "Contact us at hello@funti3r.xyz if you need another."
                )
                return render(request, 'gtm/workspace/create.html')
            workspace = Workspace.create_for_user(name=name, user=request.user)
            messages.success(request, f'Workspace "{name}" created successfully')
            return _hub_redirect(workspace.id)
        messages.error(request, 'Workspace name is required')
    return render(request, 'gtm/workspace/create.html')


@workspace_required
def workspace_detail(request, workspace_id):
    return _hub_redirect(workspace_id)


@workspace_required
@admin_or_manager_required
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
                return redirect(dashboard_url)

            # If the invitee already has an account, also surface an in-app
            # notification (they may not see the email right away).
            from django.contrib.auth import get_user_model
            from dashboard.utils_notifications import send_notification
            existing_user = get_user_model().objects.filter(email__iexact=email).first()
            if existing_user:
                send_notification(
                    recipient=existing_user,
                    sender=request.user,
                    workspace=workspace,
                    notification_type='invite',
                    level='info',
                    title="Workspace Invitation",
                    message=f"{request.user.get_full_name() or request.user.username} invited you to join {workspace.name}.",
                    link=reverse('gtm:workspace:join', args=[invitation.token])
                )

        else:
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.capitalize()}: {error}")
            
    return redirect(dashboard_url)


@workspace_permission_required('can_manage_integrations', workspace_param='workspace_id')
def workspace_integrations(request, workspace_id):
    return _hub_redirect(workspace_id)


@login_required
def workspace_join(request, token):
    """Join workspace via invitation"""
    invitation = get_object_or_404(WorkspaceInvitation, token=token, accepted_at__isnull=True)
    
    if invitation.expires_at < timezone.now():
        messages.error(request, 'This invitation has expired')
        return _hub_redirect()
    
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
        
        return _hub_redirect(invitation.workspace.id)
    
    return render(request, 'gtm/workspace/join.html', {'invitation': invitation})