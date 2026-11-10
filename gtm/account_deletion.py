"""
Permanent deletion of a user account or a workspace.

Rules (decided 2026-11-10):
- An admin cannot delete their account while a workspace they run has other
  members and no other admin. They must make someone else admin, or delete
  the workspace, first. Nobody loses a shared workspace by surprise.
- A workspace where the user is the only active member is deleted with them.
- Shared workspaces keep everything. Assessments and gap metrics the user
  created there are detached (user set to NULL), not deleted. The plain
  CASCADE on AssessmentSession.user and GapAnalysisMetric.user would otherwise
  take the workspace's assessments, tasks and gaps down with the account.
- Uploaded files are removed from storage by gtm/storage_cleanup.py as their
  rows are deleted.
"""
from django.db import transaction

from dashboard.models import GapAnalysisMetric
from .models import AssessmentSession
from .models_workspace import WorkspaceMembership


def workspaces_blocking_deletion(user):
    """Workspaces the user administers that would be left with members but no admin."""
    blocking = []
    admin_memberships = WorkspaceMembership.objects.filter(
        user=user, role="admin", is_active=True, workspace__is_active=True,
    ).select_related("workspace")
    for membership in admin_memberships:
        others = WorkspaceMembership.objects.filter(
            workspace=membership.workspace, is_active=True).exclude(user=user)
        if others.exists() and not others.filter(role="admin").exists():
            blocking.append(membership.workspace)
    return blocking


def sole_member_workspaces(user):
    """Workspaces that would be left with no active members, and so go with the account."""
    workspaces = []
    for membership in WorkspaceMembership.objects.filter(user=user, is_active=True).select_related("workspace"):
        if not WorkspaceMembership.objects.filter(
                workspace=membership.workspace, is_active=True).exclude(user=user).exists():
            workspaces.append(membership.workspace)
    return workspaces


class AccountDeletionBlocked(Exception):
    def __init__(self, workspaces):
        self.workspaces = workspaces
        super().__init__("Account deletion blocked by workspaces without another admin.")


def delete_workspace(workspace):
    """Delete a workspace and everything in it, permanently."""
    workspace.delete()


@transaction.atomic
def delete_account(user):
    blocking = workspaces_blocking_deletion(user)
    if blocking:
        raise AccountDeletionBlocked(blocking)

    for workspace in sole_member_workspaces(user):
        delete_workspace(workspace)

    AssessmentSession.objects.filter(user=user, workspace__isnull=False).update(user=None)
    GapAnalysisMetric.objects.filter(user=user, workspace__isnull=False).update(user=None)
    user.delete()
