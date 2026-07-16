"""
Regression test for the Dashboard "Top Tool Recommendations" relevance fix
(dashboard/analytics.py): recommendations must be matched to the workspace's
weakest-scoring categories from its latest completed assessment, not an
unfiltered "first 5 rows" query.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models import AssessmentSession, Category, ResultSnapshot, ToolRecommendation
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class TopToolRecommendationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="tool-rec-user", password="pass1234", email="tr@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Tool Rec Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )
        cls.weak_category = Category.objects.create(name="Demand", weight=0.4)
        cls.strong_category = Category.objects.create(name="Delivery", weight=0.2)

        cls.weak_tool = ToolRecommendation.objects.create(
            category=cls.weak_category, keyword="customer-type",
            description="Write down who you serve.", tools="Any CRM, Shared Doc",
        )
        cls.strong_tool = ToolRecommendation.objects.create(
            category=cls.strong_category, keyword="onboarding-flow",
            description="Document your onboarding steps.", tools="Notion, Docs",
        )

        cls.session = AssessmentSession.objects.create(
            owner_client_id="tool-rec-client", user=cls.user, workspace=cls.workspace,
            company_name="Tool Rec Co", is_completed=True,
        )
        ResultSnapshot.objects.create(
            session=cls.session,
            overall=55.0,
            category_breakdown=[
                {"name": "Demand", "avg": 35.0},
                {"name": "Delivery", "avg": 92.0},
            ],
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def test_weak_category_tool_is_included(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        groups = response.context["top_tool_groups"]
        category_names = [g["category_name"] for g in groups]
        self.assertIn("Demand", category_names)

    def test_strong_category_tool_is_excluded(self):
        response = self.client.get(reverse("dashboard"))
        groups = response.context["top_tool_groups"]
        category_names = [g["category_name"] for g in groups]
        self.assertNotIn("Delivery", category_names)
        self.assertNotContains(response, "onboarding-flow")

    def test_weak_category_gets_needs_attention_severity(self):
        response = self.client.get(reverse("dashboard"))
        groups = response.context["top_tool_groups"]
        demand_group = next(g for g in groups if g["category_name"] == "Demand")
        self.assertEqual(demand_group["severity_label"], "Needs Attention")

    def test_no_completed_assessment_yields_no_groups(self):
        AssessmentSession.objects.filter(pk=self.session.pk).update(is_completed=False)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["top_tool_groups"], [])
        self.assertContains(response, "No tool recommendations right now")
        AssessmentSession.objects.filter(pk=self.session.pk).update(is_completed=True)
