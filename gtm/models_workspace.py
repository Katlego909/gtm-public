# gtm/models_workspace.py
"""
Workspace, Team, and Role models for collaborative GTM assessment platform.
Separated from authentication (django-allauth) for clean separation of concerns.
"""

from django.db import models
from django.contrib.auth import get_user_model
from django.conf import settings
from django.utils import timezone
from datetime import timedelta
import uuid

User = get_user_model()


class Workspace(models.Model):
    """
    A workspace represents a company's collaborative space for GTM assessments.
    Each organization gets their own workspace for assessments and collaboration.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, help_text="Company/Organization name")
    slug = models.SlugField(max_length=100, unique=True)
    
    # Company details
    company_size = models.CharField(max_length=20, blank=True)
    industry = models.CharField(max_length=100, blank=True)
    website = models.URLField(blank=True)
    
    # Workspace settings
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Funti3r consultant assigned to this workspace
    funti3r_consultant = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="consultancy_workspaces",
        help_text="Funti3r team member managing this workspace"
    )
    
    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.slug:
            from django.utils.text import slugify
            base_slug = slugify(self.name) or "workspace"
            slug = base_slug
            counter = 1
            while Workspace.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    @classmethod
    def create_for_user(cls, name, user):
        """
        Atomic helper to create a workspace and assign the creator as 'admin'.
        """
        from django.db import transaction
        with transaction.atomic():
            workspace = cls.objects.create(name=name)
            WorkspaceMembership.objects.create(
                workspace=workspace,
                user=user,
                role='admin',
                is_active=True
            )
            return workspace

    @classmethod
    def user_at_creation_cap(cls, user) -> bool:
        """Beta safety net: unrestricted workspace creation means
        unrestricted AI-credit-account creation too (each new workspace
        gets its own fresh token budget, see gtm/ai_credits.py::
        resolve_account) -- shared by every workspace-creation entry point
        (gtm/views_workspace.py, dashboard/views/pages.py) so the cap can't
        be bypassed by using a different one. Staff are exempt so the
        internal team keeps unrestricted workspace creation for testing."""
        from django.conf import settings
        if getattr(user, "is_staff", False):
            return False
        max_workspaces = getattr(settings, "MAX_WORKSPACES_PER_USER", 1)
        owned_count = WorkspaceMembership.objects.filter(
            user=user, role='admin', is_active=True
        ).count()
        return owned_count >= max_workspaces

    def __str__(self):
        return self.name


class WorkspaceMembership(models.Model):
    """
    Links users to workspaces with specific roles.
    A user can be in multiple workspaces with different roles.
    """
    ROLE_CHOICES = [
        ('admin', 'Workspace Admin'),      # Client executive, full control
        ('manager', 'Manager'),            # Team lead, can assign tasks
        ('contributor', 'Contributor'),    # Team member, can complete tasks
        ('viewer', 'Viewer'),             # Read-only access
        ('funti3r_consultant', 'Funti3r Consultant'),  # External advisor
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    
    # Invitation and status
    invited_by = models.ForeignKey(
        User, 
        on_delete=models.SET_NULL, 
        null=True, blank=True,
        related_name="sent_invitations"
    )
    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    
    class Meta:
        unique_together = ['user', 'workspace']
        ordering = ['-invited_at']
        
    def __str__(self):
        return f"{self.user.username} - {self.workspace.name} ({self.role})"
    
    @property
    def can_invite_users(self):
        """Check if user can invite others to workspace"""
        return self.role in ['admin', 'manager', 'funti3r_consultant']
    
    @property
    def can_assign_tasks(self):
        """Check if user can assign tasks to others"""
        return self.role in ['admin', 'manager', 'funti3r_consultant']

    @property
    def can_manage_integrations(self):
        """Check if user can configure/revoke workspace integrations (CRM credentials, etc.)"""
        return self.role in ['admin', 'funti3r_consultant']

    @property
    def can_edit_workspace(self):
        """Check if user can edit workspace details (name, industry, company size, website)"""
        return self.role in ['admin', 'funti3r_consultant']

    @property
    def can_delete_workspace(self):
        """Check if user can delete (deactivate) the workspace"""
        return self.role == 'admin'

    @property
    def can_manage_ai_credits(self):
        """Check if user can view/adjust this workspace's AI credit budget."""
        return self.role in ['admin', 'funti3r_consultant']


class WorkspaceInvitation(models.Model):
    """
    Pending invitations to join a workspace.
    Allows email-based invitations before user registers.
    """
    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE)
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=WorkspaceMembership.ROLE_CHOICES)
    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE)
    invited_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()
    
    is_accepted = models.BooleanField(default=False)
    
    def save(self, *args, **kwargs):
        if not self.id:  # Set expiration only on creation
            self.expires_at = timezone.now() + timedelta(days=7)
        super().save(*args, **kwargs)

    class Meta:
        unique_together = ['workspace', 'email']
        ordering = ['-invited_at']
        
    def __str__(self):
        return f"Invite {self.email} to {self.workspace.name}"
    
    @property
    def is_expired(self):
        from django.utils import timezone
        return timezone.now() > self.expires_at


class WorkspaceActivityEvent(models.Model):
    """Lightweight workspace activity log for collaboration visibility."""

    EVENT_CHOICES = [
        ('task_created', 'Task created'),
        ('task_moved', 'Task moved'),
        ('task_deleted', 'Task deleted'),
        ('comment_added', 'Comment added'),
        ('comment_deleted', 'Comment deleted'),
        ('resource_created', 'Resource created'),
        ('resource_updated', 'Resource updated'),
        ('resource_deleted', 'Resource deleted'),
        ('client_summary_generated', 'Client summary generated'),
    ]

    workspace = models.ForeignKey(
        Workspace,
        on_delete=models.CASCADE,
        related_name='activity_events',
    )
    session = models.ForeignKey(
        'AssessmentSession',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='workspace_activity_events',
    )
    actor = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='workspace_activity_events',
    )
    event_type = models.CharField(max_length=32, choices=EVENT_CHOICES)
    summary = models.CharField(max_length=255)
    object_type = models.CharField(max_length=32, blank=True, default='')
    object_id = models.CharField(max_length=64, blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['workspace', '-created_at']),
            models.Index(fields=['event_type', '-created_at']),
            models.Index(fields=['actor', '-created_at']),
        ]

    def __str__(self):
        return f"{self.workspace.name}: {self.summary}"