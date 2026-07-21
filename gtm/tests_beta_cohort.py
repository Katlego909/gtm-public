from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models import AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class BetaCohortViewTests(TestCase):
    def setUp(self):
        self.staff_user = User.objects.create_user(
            username="staffcohort", password="pass1234", email="staff@test.com", is_staff=True,
        )
        self.regular_user = User.objects.create_user(
            username="regularcohort", password="pass1234", email="reg@test.com",
        )
        self.workspace = Workspace.objects.create(name="Cohort Co")
        WorkspaceMembership.objects.create(
            workspace=self.workspace, user=self.regular_user, role="admin", is_active=True,
        )
        AssessmentSession.objects.create(
            owner_client_id="cohort-client", user=self.regular_user, workspace=self.workspace,
            company_name="Cohort Co", is_completed=True,
        )

    def test_staff_can_view_cohort_page(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("gtm:beta_cohort"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "regularcohort")
        self.assertContains(response, "Cohort Co (admin)")

    def test_non_staff_is_denied(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse("gtm:beta_cohort"))
        self.assertNotEqual(response.status_code, 200)

    def test_anonymous_is_redirected_to_login(self):
        response = self.client.get(reverse("gtm:beta_cohort"))
        self.assertEqual(response.status_code, 302)

    def test_completed_assessment_count_is_correct(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("gtm:beta_cohort"))
        self.assertContains(response, "Cohort Co (admin)")
        # one completed assessment for regularcohort
        self.assertContains(response, "<td class=\"px-4 py-3 text-gray-600\">1</td>")
