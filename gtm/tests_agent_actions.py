"""
Tests for gtm/agent_actions.py -- the shared tool set that lets an agent
notify a teammate, comment on a task, assign a task, escalate a gap, or
recommend a resource. These are plain Python functions dispatched by Gemini's
automatic function calling, so they're tested directly here without a live
Gemini call, matching the existing convention for agent-adjacent logic (see
dashboard/tests_smoke.py's ActionItemAICompletionTests).
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from gtm.agent_actions import build_agent_action_tools
from gtm.models import ActionItem, ActionItemComment, AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership
from dashboard.models import AIResourceRecommendation, GapAnalysisMetric, Notification, Resource

User = get_user_model()


class AgentActionToolsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.actor = User.objects.create_user(
            username="agent-actor", password="pass1234", email="actor@test.com"
        )
        cls.teammate = User.objects.create_user(
            username="teammate", password="pass1234", email="teammate@test.com",
            first_name="Team", last_name="Mate",
        )
        cls.outsider = User.objects.create_user(
            username="outsider", password="pass1234", email="outsider@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Agent Actions Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.actor, role="admin", is_active=True
        )
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.teammate, role="contributor", is_active=True
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="agent-actions-client",
            user=cls.actor,
            workspace=cls.workspace,
            company_name="Agent Actions Co",
        )
        cls.task = ActionItem.objects.create(
            workspace=cls.workspace, note="Define ICP criteria", status="todo"
        )
        cls.resource = Resource.objects.create(
            workspace=cls.workspace, name="Sales Playbook", description="How to sell"
        )

    def _tools(self, user=None):
        tools = build_agent_action_tools(self.workspace, user or self.actor)
        return {t.__name__: t for t in tools}

    def test_notify_teammate_creates_notification(self):
        tools = self._tools()
        result = tools["notify_teammate"]("teammate", "Heads up", "Check the latest report")
        self.assertIn("Notified", result)
        notification = Notification.objects.get(recipient=self.teammate, notification_type='ai_report')
        self.assertEqual(notification.sender, self.actor)
        self.assertEqual(notification.title, "Heads up")

    def test_notify_teammate_outside_workspace_fails_gracefully(self):
        tools = self._tools()
        result = tools["notify_teammate"]("outsider", "Heads up", "msg")
        self.assertIn("couldn't find", result)
        self.assertFalse(Notification.objects.filter(recipient=self.outsider).exists())

    def test_comment_on_task_creates_comment_and_logs_activity(self):
        tools = self._tools()
        result = tools["comment_on_task"](str(self.task.id), "Found a blocker here.")
        self.assertIn("Commented", result)
        comment = ActionItemComment.objects.get(action_item=self.task)
        self.assertEqual(comment.user, self.actor)
        self.assertEqual(comment.text, "Found a blocker here.")

    def test_comment_on_task_outside_workspace_fails_gracefully(self):
        other_workspace = Workspace.objects.create(name="Other Co")
        other_task = ActionItem.objects.create(workspace=other_workspace, note="Not yours", status="todo")
        tools = self._tools()
        result = tools["comment_on_task"](str(other_task.id), "Sneaky comment")
        self.assertIn("couldn't find", result)
        self.assertFalse(ActionItemComment.objects.filter(action_item=other_task).exists())

    def test_assign_task_respects_can_assign_tasks_permission(self):
        # teammate is a 'contributor' -- can_assign_tasks is False for that role
        tools = self._tools(user=self.teammate)
        result = tools["assign_task"](str(self.task.id), "teammate")
        self.assertIn("don't have permission", result)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_to)

    def test_assign_task_succeeds_for_admin(self):
        tools = self._tools()  # actor is 'admin' -- can_assign_tasks is True
        result = tools["assign_task"](str(self.task.id), "teammate")
        self.assertIn("Assigned", result)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_to, self.teammate)

    def test_escalate_gap_creates_pending_metric_without_action_items(self):
        tools = self._tools()
        result = tools["escalate_gap"](
            "Marketing ROI", "CAC Payback Period", 11.5, 9.0,
            "Reduce spend on underperforming channels.",
        )
        self.assertIn("Flagged", result)
        metric = GapAnalysisMetric.objects.get(workspace=self.workspace, metric="CAC Payback Period")
        self.assertEqual(metric.source, "AI")
        self.assertEqual(ActionItem.objects.filter(workspace=self.workspace).count(), 1)  # only the seeded task

    def test_escalate_gap_rejects_invalid_category(self):
        tools = self._tools()
        result = tools["escalate_gap"]("Not A Real Category", "Win Rate", 1, 2, "x")
        self.assertIn("recognized gap category", result)
        self.assertFalse(GapAnalysisMetric.objects.filter(workspace=self.workspace).exists())

    def test_recommend_resource_creates_recommendation(self):
        tools = self._tools()
        result = tools["recommend_resource"]("Sales Playbook", str(self.session.uuid), "Matches their gap.")
        self.assertIn("Recommended", result)
        rec = AIResourceRecommendation.objects.get(session=self.session, resource=self.resource)
        self.assertEqual(rec.rationale, "Matches their gap.")

    def test_recommend_resource_unknown_resource_fails_gracefully(self):
        tools = self._tools()
        result = tools["recommend_resource"]("Nonexistent Deck", str(self.session.uuid), "x")
        self.assertIn("couldn't find a resource", result)
        self.assertFalse(AIResourceRecommendation.objects.filter(session=self.session).exists())
