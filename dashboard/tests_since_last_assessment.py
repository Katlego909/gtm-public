"""
Regression tests for the dashboard's "Since Last Assessment" stat tile
(dashboard/analytics.py::get_dashboard_context). The delta computation
itself (matching rules, category/pattern diffing) is already exhaustively
covered by gtm/tests_score_comparison.py -- these tests only confirm
get_dashboard_context calls build_score_comparison correctly and threads
the result into assessment_stats.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from gtm.models import AssessmentSession, Category, ResultSnapshot
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class SinceLastAssessmentStatTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="dashboard-stat-user", password="pass1234", email="dsu@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Stat Tile Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )
        Category.objects.create(name="Demand", weight=0.4)

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def _make_completed_session(self, *, created_at, overall, company_name="Stat Tile Co"):
        session = AssessmentSession.objects.create(
            owner_client_id="stat-tile-client", user=self.user, workspace=self.workspace,
            company_name=company_name, is_completed=True,
        )
        AssessmentSession.objects.filter(pk=session.pk).update(created_at=created_at)
        ResultSnapshot.objects.create(
            session=session, overall=overall,
            category_breakdown=[{"category": "Demand", "avg": overall / 20}],
        )
        return session

    def test_two_qualifying_sessions_yields_matching_delta(self):
        now = timezone.now()
        self._make_completed_session(created_at=now - timedelta(days=10), overall=40.0)
        self._make_completed_session(created_at=now, overall=55.0)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["assessment_stats"]["since_last_delta"], 15.0)

    def test_single_session_yields_none_not_zero(self):
        now = timezone.now()
        self._make_completed_session(created_at=now, overall=55.0)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["assessment_stats"]["since_last_delta"])

    def test_no_completed_sessions_leaves_assessment_stats_none(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["assessment_stats"])

    def test_analytics_page_shows_same_delta(self):
        now = timezone.now()
        self._make_completed_session(created_at=now - timedelta(days=10), overall=40.0)
        self._make_completed_session(created_at=now, overall=55.0)

        response = self.client.get(reverse("analytics"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["assessment_stats"]["since_last_delta"], 15.0)


@override_settings(SECURE_SSL_REDIRECT=False)
class SinceLastAssessmentSoloUserTests(TestCase):
    """No workspace at all -- dashboard falls back to AssessmentSession
    filtered by user (see get_dashboard_context's `else` branch), which
    should still flow through build_score_comparison's company_name
    matching rule for solo/no-workspace sessions."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="solo-stat-user", password="pass1234", email="ssu@test.com"
        )
        Category.objects.create(name="Demand", weight=0.4)

    def setUp(self):
        self.client.force_login(self.user)

    def _make_completed_session(self, *, created_at, overall, company_name):
        session = AssessmentSession.objects.create(
            owner_client_id="solo-stat-client", user=self.user, workspace=None,
            company_name=company_name, is_completed=True,
        )
        AssessmentSession.objects.filter(pk=session.pk).update(created_at=created_at)
        ResultSnapshot.objects.create(
            session=session, overall=overall,
            category_breakdown=[{"category": "Demand", "avg": overall / 20}],
        )
        return session

    def test_non_matching_company_name_yields_none(self):
        now = timezone.now()
        self._make_completed_session(created_at=now - timedelta(days=10), overall=40.0, company_name="Acme Corp")
        self._make_completed_session(created_at=now, overall=55.0, company_name="Globex Inc")

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["assessment_stats"]["since_last_delta"])
