"""
Tests for gtm/views/actions.py::delete_assessment -- the permanent-delete
action for completed assessments on /history/. Mirrors cancel_assessment's
existing behavior (session.delete() + require_session_ownership) but only
for completed sessions.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models import ActionItem, AssessmentSession, ResultSnapshot
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class DeleteAssessmentTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="del-owner", password="pass1234", email="owner@test.com")
        self.teammate = User.objects.create_user(username="del-teammate", password="pass1234", email="teammate@test.com")
        self.outsider = User.objects.create_user(username="del-outsider", password="pass1234", email="outsider@test.com")

        self.workspace = Workspace.objects.create(name="Delete Assessment Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.owner, role="admin", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.teammate, role="contributor", is_active=True)

        self.completed = AssessmentSession.objects.create(
            owner_client_id="delete-client", user=self.owner, workspace=self.workspace,
            company_name="Acme Inc", is_completed=True,
        )
        ResultSnapshot.objects.create(session=self.completed, overall=50, category_breakdown={})
        ActionItem.objects.create(session=self.completed, workspace=self.workspace, note="Follow up", status="todo")

        self.in_progress = AssessmentSession.objects.create(
            owner_client_id="delete-client", user=self.owner, workspace=self.workspace,
            company_name="Draft Co", is_completed=False,
        )

    def _url(self, session):
        return reverse("gtm:delete_assessment", args=[session.uuid])

    def test_owner_can_delete_completed_assessment(self):
        self.client.force_login(self.owner)
        response = self.client.post(self._url(self.completed), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssessmentSession.objects.filter(pk=self.completed.pk).exists())
        self.assertFalse(ResultSnapshot.objects.filter(session_id=self.completed.pk).exists())
        self.assertFalse(ActionItem.objects.filter(session_id=self.completed.pk).exists())

    def test_workspace_member_can_delete_completed_assessment(self):
        """Matches the existing Cancel permission model: any active workspace
        member can act on a session in their workspace, not just its owner."""
        self.client.force_login(self.teammate)
        response = self.client.post(self._url(self.completed), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AssessmentSession.objects.filter(pk=self.completed.pk).exists())

    def test_outsider_cannot_delete(self):
        self.client.force_login(self.outsider)
        response = self.client.post(self._url(self.completed), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(AssessmentSession.objects.filter(pk=self.completed.pk).exists())

    def test_in_progress_assessment_is_not_deleted(self):
        self.client.force_login(self.owner)
        response = self.client.post(self._url(self.in_progress), follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(AssessmentSession.objects.filter(pk=self.in_progress.pk).exists())

    def test_get_request_is_rejected(self):
        self.client.force_login(self.owner)
        response = self.client.get(self._url(self.completed))

        self.assertEqual(response.status_code, 405)
        self.assertTrue(AssessmentSession.objects.filter(pk=self.completed.pk).exists())
