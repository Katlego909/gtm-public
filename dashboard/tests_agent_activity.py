"""The Impact page's Agent Activity chart (dashboard/agent_value.py) must count
every kind of agent action -- not just chat and documents -- and only work
that actually happened."""
from django.test import TestCase
from django.utils import timezone

from dashboard.agent_value import ACTIVITY_GROUPS, ACTIVITY_ROWS, get_agent_value_context
from gtm.models_agent_runs import AgentRun
from gtm.models_automations import Automation, AutomationRun
from gtm.models_workspace import Workspace


class AgentActivityTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Activity Co")

    def _counts(self):
        rows = get_agent_value_context(self.workspace)["measured"]["activity_rows"]
        return {row["label"]: row["count"] for row in rows}

    def test_completed_run_steps_count_by_kind(self):
        AgentRun.objects.create(
            workspace=self.workspace, agent_type="remy", goal="Close the win-rate gap", status="completed",
            plan=[
                {"id": 1, "kind": "create_subtasks", "status": "done"},
                {"id": 2, "kind": "flag_gap", "status": "done"},
                {"id": 3, "kind": "crm_update", "status": "done"},
                {"id": 4, "kind": "notify_teammate", "status": "failed"},
                {"id": 5, "kind": "assign_task", "status": "pending"},
            ],
        )
        counts = self._counts()
        self.assertEqual(counts["Subtasks created"], 1)
        self.assertEqual(counts["Gaps flagged"], 1)
        self.assertEqual(counts["CRM updates"], 1)
        # Failed and unfinished steps did nothing, so they don't count.
        self.assertEqual(counts["Teammates notified"], 0)
        self.assertEqual(counts["Tasks assigned"], 0)

    def test_only_automation_runs_that_acted_count(self):
        automation = Automation.objects.create(
            workspace=self.workspace, name="Weekly nudge", trigger="schedule", action="create_task",
            next_run_at=timezone.now(),
        )
        AutomationRun.objects.create(automation=automation, outcome="acted")
        AutomationRun.objects.create(automation=automation, outcome="skipped")
        AutomationRun.objects.create(automation=automation, outcome="failed")
        self.assertEqual(self._counts()["Automation actions"], 1)

    def test_every_row_has_a_known_group_and_empty_workspace_is_all_zero(self):
        self.assertTrue(all(group in ACTIVITY_GROUPS for _, _, group in ACTIVITY_ROWS))
        counts = self._counts()
        self.assertEqual(len(counts), len(ACTIVITY_ROWS))
        self.assertFalse(any(counts.values()))
        no_workspace = get_agent_value_context(None)["measured"]["activity_rows"]
        self.assertEqual(len(no_workspace), len(ACTIVITY_ROWS))
