"""
Phase 2/3 of agent runs: measurement proposals (an AI-computed value only
counts once a person confirms it), automations created through an approved
plan and run by the scheduled job, the stale-run sweep, and Remy (GTM
Engineer) wired in everywhere an agent must be.
"""

import datetime
import shutil
import tempfile
from unittest import mock

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.models import GapAnalysisMetric, GapMeasurementProposal, Notification
from gtm import agent_runs
from gtm.agent_runs import RunDecisionError, approve_run, confirm_measurement, reject_measurement
from gtm.automations import AutomationError, create_automation, run_due_automations, sweep_stale_runs
from gtm.models import ActionItem, AgentRun, Automation, DeliveryDocument
from gtm.tests_agent_runs import AgentRunTestBase, _propose

MEDIA_ROOT = tempfile.mkdtemp()
DEALS_CSV = "Deal,Stage,Amount\nA,Closed Won,100\nB,Closed Lost,50\nC,Closed Won,200\nD,Closed Lost,10\n"


@override_settings(MEDIA_ROOT=MEDIA_ROOT, STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
})
class MeasurementProposalTests(AgentRunTestBase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.metric = GapAnalysisMetric.objects.create(
            workspace=self.workspace, user=self.admin, category="Sales Efficiency", metric="Win Rate",
            current=0, target=40, priority="High", recommendation="Tighten qualification.",
        )
        self.deals = DeliveryDocument.objects.create(
            session=self.session, original_filename="deals.csv", file_type="csv",
            file=ContentFile(DEALS_CSV.encode(), name="deals.csv"),
        )

    def _measure(self):
        self._use(_propose(("propose_measurement", "Measure win rate from the deals export")))
        run = self._start()
        run.refresh_from_db()

        def execute(tools, config):
            self.result = tools["propose_measurement"](
                metric_name="Win Rate", file_id=str(self.deals.pk), operation="ratio",
                filters=[{"column": "Stage", "op": "eq", "value": "Closed Won"}],
                denominator_filters=[{"column": "Stage", "op": "contains", "value": "Closed"}],
            )
            return "Proposed the win rate."
        self.client.scripts.append(execute)
        approve_run(run, self.admin, ["s1"])
        run.refresh_from_db()
        return run, GapMeasurementProposal.objects.get(run=run)

    def test_value_is_computed_from_the_file_and_waits_for_confirmation(self):
        run, proposal = self._measure()
        self.assertEqual(run.status, "completed")
        self.assertEqual(proposal.value, 50.0)
        self.assertIn("2 of 4 rows", proposal.method)
        self.assertEqual(proposal.status, "pending")
        self.metric.refresh_from_db()
        self.assertEqual(self.metric.current, 0)
        self.assertFalse(self.metric.measurements.exists())

    def test_confirming_records_an_evidence_measurement_and_can_close_the_gap(self):
        _, proposal = self._measure()
        closed = confirm_measurement(proposal, self.admin)
        self.metric.refresh_from_db()
        measurement = self.metric.measurements.get()
        self.assertEqual(measurement.source, "evidence")
        self.assertEqual(measurement.value, 50.0)
        self.assertEqual(measurement.recorded_by, self.admin)
        self.assertTrue(closed)
        self.assertEqual(self.metric.status, "closed")
        proposal.refresh_from_db()
        self.assertEqual(proposal.measurement, measurement)

    def test_rejecting_records_nothing_and_contributors_cannot_confirm(self):
        _, proposal = self._measure()
        with self.assertRaises(RunDecisionError):
            confirm_measurement(proposal, self.teammate)
        reject_measurement(proposal, self.admin)
        self.assertFalse(self.metric.measurements.exists())
        with self.assertRaises(RunDecisionError):
            confirm_measurement(proposal, self.admin)

    def test_confirm_through_the_run_panel(self):
        run, proposal = self._measure()
        http = self.client_class()
        http.force_login(self.admin)
        panel = http.get(reverse("agent_run_panel", args=[run.id]))
        self.assertContains(panel, "Measurements to confirm")
        response = http.post(reverse("agent_run_confirm_measurement", args=[run.id, proposal.id]))
        self.assertEqual(response["HX-Trigger"], "agentRunChanged")
        self.assertEqual(self.metric.measurements.get().source, "evidence")


class AutomationTests(AgentRunTestBase):
    def setUp(self):
        super().setUp()
        self.metric = GapAnalysisMetric.objects.create(
            workspace=self.workspace, user=self.admin, category="Sales Efficiency", metric="Win Rate",
            current=20, target=40, priority="High", recommendation="Tighten qualification.",
        )

    def _automation(self, **overrides):
        kwargs = dict(
            workspace=self.workspace, session=self.session, name="Win rate check", trigger="metric_off_target",
            frequency="weekly", action="create_task",
            config={"metric": "Win Rate", "note": "Review lost deals", "assignee": "Priya", "due_in_days": 3},
            created_by=self.admin,
        )
        kwargs.update(overrides)
        return create_automation(**kwargs)

    def test_invalid_specs_are_rejected_with_reasons(self):
        with self.assertRaisesMessage(AutomationError, "can only notify"):
            self._automation(trigger="tasks_overdue")
        with self.assertRaisesMessage(AutomationError, "no teammate matches: Nobody"):
            self._automation(action="notify", config={"metric": "Win Rate", "recipients": ["Nobody"], "message": "x"})
        with self.assertRaisesMessage(AutomationError, "no 'Net Revenue Retention' gap"):
            self._automation(config={"metric": "Net Revenue Retention", "note": "x"})

    def test_off_target_metric_creates_a_dated_task_once_per_period(self):
        automation = self._automation()
        later = automation.next_run_at + datetime.timedelta(minutes=1)
        self.assertEqual(run_due_automations(now=later)["acted"], 1)
        task = ActionItem.objects.get(note__startswith="Review lost deals")
        self.assertEqual(task.assigned_to, self.teammate)
        self.assertIsNotNone(task.due_date)
        # Same moment again: already claimed for this period, nothing repeats.
        self.assertEqual(run_due_automations(now=later), {"acted": 0, "skipped": 0, "failed": 0})
        automation.refresh_from_db()
        self.assertGreater(automation.next_run_at, later)

    def test_on_target_metric_is_skipped(self):
        automation = self._automation()
        GapAnalysisMetric.objects.filter(pk=self.metric.pk).update(current=45)
        counts = run_due_automations(now=automation.next_run_at + datetime.timedelta(minutes=1))
        self.assertEqual(counts["skipped"], 1)
        self.assertFalse(ActionItem.objects.filter(note__startswith="Review lost deals").exists())

    def test_paused_automations_do_not_run(self):
        automation = self._automation()
        Automation.objects.filter(pk=automation.pk).update(is_active=False)
        counts = run_due_automations(now=automation.next_run_at + datetime.timedelta(days=1))
        self.assertEqual(counts, {"acted": 0, "skipped": 0, "failed": 0})

    def test_overdue_tasks_notify_admins(self):
        self.item.due_date = timezone.localdate() - datetime.timedelta(days=2)
        self.item.save()
        automation = self._automation(
            name="Overdue sweep", trigger="tasks_overdue", action="notify",
            config={"recipients": ["admins"], "message": "These tasks are overdue."},
        )
        run_due_automations(now=automation.next_run_at + datetime.timedelta(minutes=1))
        note = Notification.objects.get(recipient=self.admin, title="Overdue sweep")
        self.assertIn("Define our ICP", note.message)

    def test_agent_creates_an_automation_only_through_an_approved_step(self):
        self._use(_propose(("create_automation", "Weekly win-rate check")))
        run = self._start()
        run.refresh_from_db()

        def execute(tools, config):
            tools["create_automation"](
                name="Weekly win-rate check", trigger="metric_off_target", action="notify", frequency="weekly",
                metric_name="Win Rate", recipients=["admins"], message="Win rate is below target.",
            )
            return "Set it up."
        self.client.scripts.append(execute)
        approve_run(run, self.admin, ["s1"])
        run.refresh_from_db()
        automation = Automation.objects.get(created_by_run=run)
        self.assertEqual(run.status, "completed")
        self.assertEqual(automation.approved_by, self.admin)
        self.assertEqual(automation.agent_type, "gtm_strategist")

    def test_management_command_runs_due_automations(self):
        automation = self._automation()
        Automation.objects.filter(pk=automation.pk).update(next_run_at=timezone.now() - datetime.timedelta(minutes=1))
        call_command("run_automations", stdout=mock.MagicMock())
        self.assertTrue(ActionItem.objects.filter(note__startswith="Review lost deals").exists())

    def test_stale_runs_are_failed_so_they_can_be_retried(self):
        run = AgentRun.objects.create(
            agent_type="gtm_strategist", goal="x", status="running", requested_by=self.admin,
            session=self.session, workspace=self.workspace, action_item=self.item,
        )
        AgentRun.objects.filter(pk=run.pk).update(updated_at=timezone.now() - datetime.timedelta(hours=1))
        self.assertEqual(sweep_stale_runs(), 1)
        run.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(self.item.ai_run_state, "failed")

    def test_automations_panel_lets_admins_pause_and_shows_contributors_read_only(self):
        automation = self._automation()
        http = self.client_class()
        http.force_login(self.teammate)
        response = http.get(reverse("automations_panel") + f"?workspace={self.workspace.id}")
        self.assertContains(response, "Win rate check")
        self.assertNotContains(response, "Pause")
        http.force_login(self.admin)
        http.post(reverse("automation_toggle", args=[automation.id]) + f"?workspace={self.workspace.id}")
        automation.refresh_from_db()
        self.assertFalse(automation.is_active)


class RemyTests(AgentRunTestBase):
    def test_remy_is_registered_everywhere_an_agent_must_be(self):
        from gtm.agent_runtime import AGENT_DIRECTORY
        from gtm.models import ActionItem as Item
        from gtm.team_chat import TEAM_AGENT_TYPES, _build_transfer_tools
        from gtm.workspace_agent_chat import AGENT_TYPES, SUGGESTED_PROMPTS, SYSTEM_INSTRUCTIONS

        self.assertEqual(AGENT_DIRECTORY["gtm_engineer"]["name"], "Remy")
        self.assertIn("gtm_engineer", AGENT_TYPES)
        self.assertIn("gtm_engineer", TEAM_AGENT_TYPES)
        self.assertIn("gtm_engineer", dict(Item.AGENT_TYPE_CHOICES))
        self.assertIn("Remy", SYSTEM_INSTRUCTIONS["gtm_engineer"])
        self.assertTrue(SUGGESTED_PROMPTS["gtm_engineer"])
        transfers = {t.__name__ for t in _build_transfer_tools("insights", has_session=True)}
        self.assertIn("transfer_to_gtm_engineer_agent", transfers)

    def test_remy_chat_tools(self):
        from gtm.workspace_agent_chat import _build_workspace_tools

        self.session.is_completed = True
        self.session.save()
        names = [t.__name__ for t in _build_workspace_tools("gtm_engineer", self.workspace, user=self.admin)]
        for tool in ("get_gtm_diagnosis", "compute_metric_from_file", "get_tool_recommendations",
                     "list_automations", "start_multi_step_work", "consult_insights_agent"):
            self.assertIn(tool, names)
        self.assertEqual(len(names), len(set(names)))
        nora = [t.__name__ for t in _build_workspace_tools("portfolio", self.workspace, user=self.admin)]
        self.assertIn("consult_gtm_engineer_agent", nora)

    def test_task_routed_to_remy_runs_as_remy_with_unique_tools(self):
        self.item.assigned_agent_type = "gtm_engineer"
        self.item.save()
        self._use(_propose(("create_document", "Lead scoring spec")))
        run = start_run_for(self.item, self.admin)
        run.refresh_from_db()
        self.assertEqual(run.agent_type, "gtm_engineer")
        config = self.client.configs[0]
        self.assertIn("You are Remy", config.system_instruction)
        names = [t.__name__ for t in config.tools]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("compute_metric_from_file", names)


def start_run_for(item, user):
    from dashboard.views.actions import start_ai_completion

    start_ai_completion(item, user.id, item.workspace_id)
    item.refresh_from_db()
    return item.current_run


class WorkspaceAgentAssessmentContextTests(AgentRunTestBase):
    """Every workspace agent sees the latest completed assessment, and the
    assessment tool says plainly what it doesn't record, so an agent can't
    pass an assumption off as something the company said."""

    def test_workspace_agents_get_the_latest_assessment(self):
        from gtm.workspace_agent_chat import AGENT_TYPES, _build_workspace_tools

        self.session.is_completed = True
        self.session.save()
        for agent_type in AGENT_TYPES:
            names = {t.__name__ for t in _build_workspace_tools(agent_type, self.workspace, user=self.admin)}
            self.assertIn("get_gtm_assessment_data", names, agent_type)
            self.assertIn("get_gtm_diagnosis", names, agent_type)

    def test_missing_facts_are_reported_as_not_stated(self):
        from gtm.ai_chat import _make_get_gtm_assessment_data_tool

        report = _make_get_gtm_assessment_data_tool(self.session)()
        self.assertIn("Industry: not stated in the assessment", report)
        self.assertIn("The company added no written context", report)
        self.assertIn("never present it as something the company said", report)
