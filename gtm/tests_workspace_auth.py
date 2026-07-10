"""
Regression tests for workspace authorization and report-email dispatch.

Covers three previously-reachable bugs:
- Removed members were silently re-added on their next login, because the
  auto-join signal ran on every ``User`` save (including the ``last_login``-only
  save) and ignored invitation expiry/acceptance.
- The workspace middleware wrote ``current_workspace_id`` to the session before
  verifying membership, poisoning the session and causing an infinite redirect
  loop for non-members.
- The report email was sent multiple times because the snapshot is saved several
  times during playbook generation.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from gtm.models import AssessmentSession, ResultSnapshot
from gtm.models_workspace import (
    Workspace,
    WorkspaceInvitation,
    WorkspaceMembership,
)

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class AutoJoinSignalTests(TestCase):
    """gtm.signals.auto_join_invited_workspaces"""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="ws-admin", password="pass1234", email="admin@test.com"
        )
        self.workspace = Workspace.objects.create(name="Acme")

    def _make_invite(self, email, *, expired=False):
        invite = WorkspaceInvitation.objects.create(
            workspace=self.workspace,
            email=email,
            role="contributor",
            invited_by=self.admin,
        )
        if expired:
            # save() only sets expires_at on creation, so bypass it via update().
            WorkspaceInvitation.objects.filter(pk=invite.pk).update(
                expires_at=timezone.now() - timedelta(days=1)
            )
        return invite

    def test_removed_member_not_readded_on_login(self):
        """A last_login-only save must not resurrect a removed membership."""
        member = User.objects.create_user(
            username="member", password="pass1234", email="member@test.com"
        )
        self._make_invite("member@test.com")
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=member, role="contributor", is_active=True
        )

        # Admin removes the member.
        WorkspaceMembership.objects.filter(workspace=self.workspace, user=member).delete()

        # Django emits this exact save on every successful login.
        member.last_login = timezone.now()
        member.save(update_fields=["last_login"])

        self.assertFalse(
            WorkspaceMembership.objects.filter(workspace=self.workspace, user=member).exists(),
            "Removed member was silently re-added on login",
        )

    def test_expired_invitation_does_not_grant_access(self):
        user = User.objects.create_user(
            username="late", password="pass1234", email="late@test.com"
        )
        self._make_invite("late@test.com", expired=True)

        # A full save (e.g. profile update) still must not honor an expired invite.
        user.save()

        self.assertFalse(
            WorkspaceMembership.objects.filter(workspace=self.workspace, user=user).exists(),
            "Expired invitation granted workspace access",
        )

    def test_pending_unexpired_invitation_still_auto_joins(self):
        """The happy path must keep working: a fresh registration is auto-joined."""
        self._make_invite("newcomer@test.com")
        user = User.objects.create_user(
            username="newcomer", password="pass1234", email="newcomer@test.com"
        )
        self.assertTrue(
            WorkspaceMembership.objects.filter(workspace=self.workspace, user=user).exists(),
            "Valid pending invitation did not auto-join a new user",
        )


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceMiddlewareTests(TestCase):
    """gtm.middleware_workspace.WorkspaceMiddleware"""

    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner", password="pass1234", email="owner@test.com"
        )
        self.outsider = User.objects.create_user(
            username="outsider", password="pass1234", email="outsider@test.com"
        )
        self.workspace = Workspace.objects.create(name="Private Co")
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=self.owner, role="admin", is_active=True
        )

    def test_non_member_redirected_without_poisoning_session(self):
        self.client.force_login(self.outsider)
        url = reverse("workspace:detail", args=[self.workspace.id])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(
            self.client.session.get("current_workspace_id"),
            str(self.workspace.id),
            "Middleware stored a workspace the user cannot access (redirect-loop risk)",
        )

    def test_member_access_sets_session(self):
        self.client.force_login(self.owner)
        url = reverse("workspace:detail", args=[self.workspace.id])

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.session.get("current_workspace_id"), str(self.workspace.id)
        )


@override_settings(SECURE_SSL_REDIRECT=False)
class ReportEmailDispatchTests(TestCase):
    """gtm.signals.send_report_when_snapshot_saved"""

    def test_report_email_sent_once_across_multiple_saves(self):
        session = AssessmentSession.objects.create(
            owner_client_id="client-x", company_name="Acme", is_completed=True
        )

        with patch("gtm.signals.send_snapshot_report_email") as mock_send:
            snapshot = ResultSnapshot.objects.create(
                session=session, overall=50.0, category_breakdown=[]
            )
            # Simulate the repeated saves performed during playbook generation.
            snapshot.ai_playbook_status = "generating"
            snapshot.save(update_fields=["ai_playbook_status"])
            snapshot.ai_playbook_status = "done"
            snapshot.save(update_fields=["ai_playbook_status"])

        self.assertEqual(
            mock_send.call_count, 1, "Report email was sent more than once"
        )
