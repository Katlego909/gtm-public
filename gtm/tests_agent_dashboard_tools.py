"""
Tests for gtm/agent_dashboard_tools.py -- the shared tool set that gives an
agent read context and governed write access to dashboard-app data (gap
metrics, notifications, AI value, teammate roster). These are plain Python
functions dispatched by Gemini's automatic function calling, so they're
tested directly here without a live Gemini call, matching the existing
convention in gtm/tests_agent_actions.py.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from dashboard.models import GapAnalysisMetric, GapMetricMeasurement, Notification
from gtm.agent_dashboard_tools import build_dashboard_ambient_context, build_dashboard_tools
from gtm.models import AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


class DashboardToolsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_user(
            username="dash-actor", password="pass1234", email="actor@test.com",
            first_name="Dash", last_name="Actor",
        )
        cls.viewer = User.objects.create_user(
            username="dash-viewer", password="pass1234", email="viewer@test.com",
        )
        cls.outsider = User.objects.create_user(
            username="dash-outsider", password="pass1234", email="outsider@test.com",
        )
        cls.workspace = Workspace.objects.create(name="Dashboard Tools Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.actor, role="admin", is_active=True
        )
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.viewer, role="viewer", is_active=True
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="dash-tools-client",
            user=cls.actor,
            workspace=cls.workspace,
            company_name="Dashboard Tools Co",
        )

    def _make_metric(self, **kwargs):
        defaults = dict(
            workspace=self.workspace,
            session=self.session,
            user=self.actor,
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

    def _tools(self, workspace=None, session=None, user=None):
        tools = build_dashboard_tools(workspace=workspace, session=session, user=user)
        return {t.__name__: t for t in tools}


class LogGapMeasurementFromChatTests(DashboardToolsTestCase):
    def test_logs_measurement_with_agent_provenance(self):
        metric = self._make_metric()
        tools = self._tools(workspace=self.workspace, user=self.actor)
        result = tools["log_gap_measurement_from_chat"]("Win Rate", 38.0, "Reported in chat")
        self.assertIn("Logged", result)

        measurement = GapMetricMeasurement.objects.get(gap_metric=metric)
        self.assertEqual(measurement.value, 38.0)
        self.assertEqual(measurement.source, "agent")
        self.assertEqual(measurement.recorded_by, self.actor)
        self.assertEqual(measurement.note, "Reported in chat")

        metric.refresh_from_db()
        self.assertEqual(metric.current, 38.0)
        self.assertEqual(metric.estimate_method, "measured")
        self.assertEqual(metric.status, "open")

    def test_reaching_target_closes_gap(self):
        self._make_metric()
        tools = self._tools(workspace=self.workspace, user=self.actor)
        result = tools["log_gap_measurement_from_chat"]("Win Rate", 45.0)
        self.assertIn("closed", result.lower())

        metric = GapAnalysisMetric.objects.get(workspace=self.workspace, metric="Win Rate")
        self.assertEqual(metric.status, "closed")
        self.assertEqual(metric.closed_reason, "target_reached")

    def test_lower_is_better_metric_direction_respected(self):
        """CAC Payback Period inverts direction: a measured drop to the
        target (or below) closes the gap, a rise keeps it open."""
        self._make_metric(
            category="Marketing ROI", metric="CAC Payback Period", current=11.7, target=9.7,
        )
        tools = self._tools(workspace=self.workspace, user=self.actor)

        tools["log_gap_measurement_from_chat"]("CAC Payback Period", 12.4)
        metric = GapAnalysisMetric.objects.get(workspace=self.workspace, metric="CAC Payback Period")
        self.assertEqual(metric.status, "open")

        tools["log_gap_measurement_from_chat"]("CAC Payback Period", 9.5)
        metric.refresh_from_db()
        self.assertEqual(metric.status, "closed")

    def test_unrecognized_metric_name_fails_gracefully(self):
        tools = self._tools(workspace=self.workspace, user=self.actor)
        result = tools["log_gap_measurement_from_chat"]("Not A Real Metric", 10.0)
        self.assertIn("recognized", result)
        self.assertFalse(GapMetricMeasurement.objects.exists())

    def test_no_matching_metric_in_scope_fails_gracefully(self):
        tools = self._tools(workspace=self.workspace, user=self.actor)
        result = tools["log_gap_measurement_from_chat"]("Win Rate", 38.0)
        self.assertIn("No gap metric found", result)
        self.assertFalse(GapMetricMeasurement.objects.exists())


class WritePermissionTests(DashboardToolsTestCase):
    """A viewer-role membership (active, but no can_assign_tasks-equivalent
    write standing) still passes the *light* active-membership check every
    dashboard write tool uses -- only an outsider with no membership at all
    should be denied. Read tools stay open regardless."""

    def test_viewer_can_log_measurement_but_outsider_cannot(self):
        self._make_metric()

        viewer_tools = self._tools(workspace=self.workspace, user=self.viewer)
        result = viewer_tools["log_gap_measurement_from_chat"]("Win Rate", 38.0)
        self.assertIn("Logged", result)

        outsider_tools = self._tools(workspace=self.workspace, user=self.outsider)
        result = outsider_tools["log_gap_measurement_from_chat"]("Win Rate", 39.0)
        self.assertIn("don't have", result)
        self.assertEqual(GapMetricMeasurement.objects.count(), 1)

    def test_outsider_can_still_read(self):
        self._make_metric()
        outsider_tools = self._tools(workspace=self.workspace, user=self.outsider)
        result = outsider_tools["list_gap_metrics"]()
        self.assertIn("Win Rate", result)


class ListGapMetricsTests(DashboardToolsTestCase):
    def test_lists_open_metrics_by_default(self):
        self._make_metric()
        self._make_metric(metric="Average Deal Size", status="closed", closed_reason="manual")
        tools = self._tools(workspace=self.workspace, user=self.actor)
        result = tools["list_gap_metrics"]()
        self.assertIn("Win Rate", result)
        self.assertNotIn("Average Deal Size", result)

    def test_status_all_includes_closed(self):
        self._make_metric()
        self._make_metric(metric="Average Deal Size", status="closed", closed_reason="manual")
        tools = self._tools(workspace=self.workspace, user=self.actor)
        result = tools["list_gap_metrics"]("all")
        self.assertIn("Win Rate", result)
        self.assertIn("Average Deal Size", result)

    def test_no_scope_returns_gracefully_and_leaks_nothing(self):
        """Regression for the _gap_metric_scope_queryset user=None hardening:
        an anonymous-owned metric for one user must never surface when the
        tool is called with no workspace and no user."""
        GapAnalysisMetric.objects.create(
            workspace=None, session=None, user=self.actor,
            category="Sales Efficiency", metric="Win Rate", current=1, target=2,
            priority="Low", recommendation="x", source="USER",
        )
        tools = self._tools(workspace=None, session=None, user=None)
        result = tools["list_gap_metrics"]()
        self.assertNotIn("Win Rate", result)
        self.assertIn("No gap metrics", result)

    def test_scoped_to_owning_user_outside_workspace(self):
        GapAnalysisMetric.objects.create(
            workspace=None, session=None, user=self.actor,
            category="Sales Efficiency", metric="Win Rate", current=1, target=2,
            priority="Low", recommendation="x", source="USER",
        )
        mine = self._tools(workspace=None, user=self.actor)["list_gap_metrics"]()
        self.assertIn("Win Rate", mine)

        someone_elses = self._tools(workspace=None, user=self.outsider)["list_gap_metrics"]()
        self.assertNotIn("Win Rate", someone_elses)


class ReadMyNotificationsTests(DashboardToolsTestCase):
    def test_returns_only_calling_users_own_notifications(self):
        Notification.objects.create(
            recipient=self.actor, workspace=self.workspace,
            title="For actor", message="hello actor",
        )
        Notification.objects.create(
            recipient=self.viewer, workspace=self.workspace,
            title="For viewer", message="hello viewer",
        )
        result = self._tools(workspace=self.workspace, user=self.actor)["read_my_notifications"]()
        self.assertIn("For actor", result)
        self.assertNotIn("For viewer", result)

    def test_unread_only_filters_read_ones(self):
        Notification.objects.create(
            recipient=self.actor, workspace=self.workspace,
            title="Already read", message="x", is_read=True,
        )
        result = self._tools(workspace=self.workspace, user=self.actor)["read_my_notifications"](True)
        self.assertNotIn("Already read", result)
        result_all = self._tools(workspace=self.workspace, user=self.actor)["read_my_notifications"](False)
        self.assertIn("Already read", result_all)

    def test_no_user_returns_gracefully(self):
        tools = self._tools(workspace=self.workspace, user=None)
        result = tools["read_my_notifications"]()
        self.assertIn("not signed in", result)


class SessionWithoutWorkspaceTests(DashboardToolsTestCase):
    def test_workspace_less_session_still_gets_read_and_write_tools(self):
        standalone_session = AssessmentSession.objects.create(
            owner_client_id="standalone-client", user=self.actor, company_name="Standalone Co",
        )
        tools = self._tools(workspace=None, session=standalone_session, user=self.actor)
        for name in ("list_gap_metrics", "get_gap_metric_detail", "log_gap_measurement_from_chat", "read_my_notifications"):
            self.assertIn(name, tools)
        # Workspace-only tools are simply absent, not present-but-erroring.
        self.assertNotIn("get_ai_value_summary", tools)
        self.assertNotIn("list_teammates", tools)
        # Calling a read tool must not raise even with no workspace/session data.
        tools["list_gap_metrics"]()


class ListTeammatesTests(DashboardToolsTestCase):
    def test_lists_active_members_with_role(self):
        result = self._tools(workspace=self.workspace, user=self.actor)["list_teammates"]()
        self.assertIn("Dash Actor", result)
        self.assertIn("Workspace Admin", result)

    def test_omitted_without_workspace(self):
        tools = self._tools(workspace=None, user=self.actor)
        self.assertNotIn("list_teammates", tools)


class AmbientContextTests(DashboardToolsTestCase):
    def test_empty_for_quiet_scope(self):
        self.assertEqual(build_dashboard_ambient_context(workspace=self.workspace, user=self.actor), "")

    def test_non_empty_once_open_gap_exists(self):
        self._make_metric()
        text = build_dashboard_ambient_context(workspace=self.workspace, user=self.actor)
        self.assertIn("open GTM gap", text)

    def test_non_empty_once_unread_notification_exists(self):
        Notification.objects.create(
            recipient=self.actor, workspace=self.workspace, title="Hi", message="x",
        )
        text = build_dashboard_ambient_context(workspace=self.workspace, user=self.actor)
        self.assertIn("unread notification", text)
