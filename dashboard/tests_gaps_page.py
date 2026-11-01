"""
Gaps has its own page (the gap_report view): the drafts panel, Add Metric and
the actionable table live there. The dashboard only shows a "Gaps to close"
summary card with the furthest-behind metrics.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.models import GapAnalysisMetric
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class GapsPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="gaps-user", password="pass1234", email="gaps@test.com")
        cls.workspace = Workspace.objects.create(name="Gaps Co")
        WorkspaceMembership.objects.create(workspace=cls.workspace, user=cls.user, role="admin", is_active=True)

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def _metric(self, metric, current, target=100, **overrides):
        values = dict(
            category="Sales Efficiency", metric=metric, current=current, target=target, priority="High",
            recommendation=f"Improve {metric}.", workspace=self.workspace, user=self.user,
            source="USER", estimate_method="measured",
        )
        values.update(overrides)
        return GapAnalysisMetric.objects.create(**values)

    def test_dashboard_shows_summary_card_not_the_workflow(self):
        self._metric("Win Rate", 50)
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, "Gaps to close")
        self.assertContains(response, "Manage gaps")
        self.assertNotContains(response, 'id="gap-suggestions-panel"')
        self.assertNotContains(response, 'id="gap-analysis-table"')

    def test_dashboard_card_lists_three_furthest_behind_worst_first(self):
        self._metric("Gap A", 90)   # -10%
        self._metric("Gap B", 20)   # -80%
        self._metric("Gap C", 50)   # -50%
        self._metric("Gap D", 70)   # -30%
        self._metric("On Track", 120)
        response = self.client.get(reverse("dashboard"))
        names = [g.metric for g in response.context["top_gaps"]]
        self.assertEqual(names, ["Gap B", "Gap C", "Gap D"])
        self.assertEqual(response.context["behind_gap_count"], 4)

    def test_unmeasured_gaps_are_not_listed_as_behind(self):
        self._metric("Never Measured", 0, estimate_method="unmeasured")
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.context["top_gaps"], [])
        self.assertContains(response, "Nothing is behind target right now.")

    def test_gaps_page_has_the_full_workflow(self):
        self._metric("Win Rate", 50)
        response = self.client.get(reverse("gap_report"))
        self.assertContains(response, 'id="gap-suggestions-panel"')
        self.assertContains(response, 'id="gap-analysis-table"')
        self.assertContains(response, reverse("add_gap_metric"))
        self.assertContains(response, "Improve Win Rate.")

    def test_sidebar_links_to_gaps_page(self):
        response = self.client.get(reverse("dashboard"))
        self.assertContains(response, f'href="{reverse("gap_report")}"')
