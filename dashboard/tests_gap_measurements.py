"""
Tests for the gap-metric measurement/closure loop: GapMetricMeasurement
check-ins (dashboard/views/gap_analysis.py::log_gap_measurement), the
direction-aware auto-close on measured target (helpers.apply_measured_closure),
manual resolve/reopen endpoints, and the deterministic pillar score-movement
annotation (helpers.prepare_gap_metrics_for_display).

Design invariant under test: only measured values (or an explicit human
resolve) close a gap -- task completion and AI estimates never do.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.models import GapAnalysisMetric, GapMetricMeasurement
from dashboard.views.helpers import prepare_gap_metrics_for_display
from gtm.models import ActionItem, AssessmentSession, Category, Question, Response
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class GapMeasurementTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="gap-measure", password="pass1234", email="gap-measure@test.com"
        )
        cls.outsider = User.objects.create_user(
            username="gap-outsider", password="pass1234", email="gap-outsider@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Gap Measure Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="gap-measure-client",
            user=cls.user,
            workspace=cls.workspace,
            company_name="Gap Measure Co",
            is_completed=True,
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def _make_metric(self, **kwargs):
        defaults = dict(
            workspace=self.workspace,
            session=self.session,
            user=self.user,
            category="Sales Efficiency",
            metric="Win Rate",
            current=34.9,
            target=42.9,
            priority="High",
            recommendation="Standardize qualification and run deal review coaching.",
            source="AI",
        )
        defaults.update(kwargs)
        return GapAnalysisMetric.objects.create(**defaults)


class LogMeasurementTests(GapMeasurementTestCase):
    def test_measurement_below_target_keeps_gap_open(self):
        metric = self._make_metric()
        response = self.client.post(
            reverse("log_gap_measurement", args=[metric.id]), {"value": "38.0"}
        )
        self.assertEqual(response.status_code, 200)

        metric.refresh_from_db()
        self.assertEqual(metric.status, "open")
        self.assertEqual(metric.current, 38.0)

        measurement = GapMetricMeasurement.objects.get(gap_metric=metric)
        self.assertEqual(measurement.value, 38.0)
        self.assertEqual(measurement.source, "manual")
        self.assertEqual(measurement.recorded_by, self.user)

    def test_measurement_reaching_target_closes_gap(self):
        metric = self._make_metric()
        response = self.client.post(
            reverse("log_gap_measurement", args=[metric.id]),
            {"value": "43.5", "note": "June CRM report"},
        )
        self.assertEqual(response.status_code, 200)

        metric.refresh_from_db()
        self.assertEqual(metric.status, "closed")
        self.assertEqual(metric.closed_reason, "target_reached")
        self.assertIsNotNone(metric.closed_at)

    def test_lower_is_better_metric_closes_when_value_drops_to_target(self):
        """CAC Payback Period inverts direction: current 11.7 vs target 9.7
        is behind; a measured drop to 9.5 reaches the target."""
        metric = self._make_metric(
            category="Marketing ROI", metric="CAC Payback Period",
            current=11.7, target=9.7,
        )
        self.client.post(reverse("log_gap_measurement", args=[metric.id]), {"value": "9.5"})

        metric.refresh_from_db()
        self.assertEqual(metric.status, "closed")
        self.assertEqual(metric.closed_reason, "target_reached")

    def test_lower_is_better_metric_stays_open_when_value_rises(self):
        metric = self._make_metric(
            category="Marketing ROI", metric="CAC Payback Period",
            current=11.7, target=9.7,
        )
        self.client.post(reverse("log_gap_measurement", args=[metric.id]), {"value": "12.4"})

        metric.refresh_from_db()
        self.assertEqual(metric.status, "open")

    def test_metric_from_another_workspace_is_not_found(self):
        other_workspace = Workspace.objects.create(name="Other Measure Co")
        metric = self._make_metric(workspace=other_workspace, user=self.outsider)

        response = self.client.post(
            reverse("log_gap_measurement", args=[metric.id]), {"value": "50"}
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(GapMetricMeasurement.objects.filter(gap_metric=metric).exists())


class ResolveReopenTests(GapMeasurementTestCase):
    def test_resolve_marks_gap_manually_closed(self):
        metric = self._make_metric()
        response = self.client.post(reverse("resolve_gap_metric", args=[metric.id]))
        self.assertEqual(response.status_code, 200)

        metric.refresh_from_db()
        self.assertEqual(metric.status, "closed")
        self.assertEqual(metric.closed_reason, "manual")
        self.assertIsNotNone(metric.closed_at)

    def test_reopen_returns_gap_to_open(self):
        metric = self._make_metric(status="closed", closed_reason="target_reached")
        response = self.client.post(reverse("reopen_gap_metric", args=[metric.id]))
        self.assertEqual(response.status_code, 200)

        metric.refresh_from_db()
        self.assertEqual(metric.status, "open")
        self.assertEqual(metric.closed_reason, "")
        self.assertIsNone(metric.closed_at)

    def test_resolve_scoped_to_workspace(self):
        other_workspace = Workspace.objects.create(name="Other Resolve Co")
        metric = self._make_metric(workspace=other_workspace, user=self.outsider)

        response = self.client.post(reverse("resolve_gap_metric", args=[metric.id]))
        self.assertEqual(response.status_code, 404)
        metric.refresh_from_db()
        self.assertEqual(metric.status, "open")


class DisplayAnnotationTests(GapMeasurementTestCase):
    def test_task_counts_and_reassessment_hint(self):
        metric = self._make_metric()
        ActionItem.objects.create(
            session=self.session, workspace=self.workspace,
            note="Tighten discovery scripts", status="done", gap_metric=metric,
        )
        ActionItem.objects.create(
            session=self.session, workspace=self.workspace,
            note="Run deal review coaching", status="done", gap_metric=metric,
        )

        prepare_gap_metrics_for_display([metric], self.workspace, self.user)

        self.assertEqual(metric.task_count, 2)
        self.assertEqual(metric.done_task_count, 2)
        # All tasks done and no assessment newer than the one that flagged
        # the gap -- the honest next step is re-measuring via re-assessment.
        self.assertTrue(metric.suggest_reassessment)
        self.assertIsNone(metric.score_movement)

    def test_score_movement_against_newer_assessment(self):
        """Deterministic pillar comparison: gap flagged from an older session
        scoring 2 on Conversion; a newer completed session scores 4 -- the row
        must expose Conversion 2.0 -> 4.0 (Sales Efficiency maps to Conversion)."""
        category = Category.objects.create(name="Conversion", weight=1.0)
        question = Question.objects.create(
            id_code="CON-TST-01", category=category,
            text="Do you qualify leads consistently?", weight=1.0,
        )
        Response.objects.create(session=self.session, question=question, score=2)

        newer_session = AssessmentSession.objects.create(
            owner_client_id="gap-measure-client",
            user=self.user,
            workspace=self.workspace,
            company_name="Gap Measure Co",
            is_completed=True,
        )
        Response.objects.create(session=newer_session, question=question, score=4)

        metric = self._make_metric()
        prepare_gap_metrics_for_display([metric], self.workspace, self.user)

        self.assertIsNotNone(metric.score_movement)
        self.assertEqual(metric.score_movement["pillar"], "Conversion")
        self.assertEqual(metric.score_movement["baseline"], 2.0)
        self.assertEqual(metric.score_movement["latest"], 4.0)
        self.assertEqual(metric.score_movement["delta"], 2.0)
        self.assertFalse(metric.suggest_reassessment)

    def test_incomplete_tasks_do_not_suggest_reassessment(self):
        metric = self._make_metric()
        ActionItem.objects.create(
            session=self.session, workspace=self.workspace,
            note="Tighten discovery scripts", status="todo", gap_metric=metric,
        )

        prepare_gap_metrics_for_display([metric], self.workspace, self.user)

        self.assertEqual(metric.task_count, 1)
        self.assertEqual(metric.done_task_count, 0)
        self.assertFalse(metric.suggest_reassessment)


class ReflagResetsClosureTests(GapMeasurementTestCase):
    def test_upsert_reopens_a_closed_metric(self):
        """Accepting a fresh AI suggestion for a metric that was previously
        closed re-flags it as an open gap."""
        from dashboard.views.helpers import _upsert_gap_metric_in_scope
        from django.utils import timezone

        metric = self._make_metric(
            status="closed", closed_reason="target_reached", closed_at=timezone.now()
        )
        updated, created = _upsert_gap_metric_in_scope(
            user=self.user,
            workspace=self.workspace,
            session=self.session,
            payload={
                "category": "Sales Efficiency",
                "metric": "Win Rate",
                "current": 36.0,
                "target": 45.0,
                "priority": "High",
                "recommendation": "New quarter, new gap.",
            },
            source="AI",
        )

        self.assertFalse(created)
        self.assertEqual(updated.pk, metric.pk)
        self.assertEqual(updated.status, "open")
        self.assertEqual(updated.closed_reason, "")
        self.assertIsNone(updated.closed_at)
