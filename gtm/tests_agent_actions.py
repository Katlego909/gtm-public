"""
Tests for gtm/agent_actions.py -- the shared tool set that lets an agent
notify a teammate, comment on a task, assign a task, escalate a gap, or
recommend a resource. These are plain Python functions dispatched by Gemini's
automatic function calling, so they're tested directly here without a live
Gemini call, matching the existing convention for agent-adjacent logic (see
dashboard/tests_smoke.py's ActionItemAICompletionTests).
"""

from unittest import mock

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

    def test_notify_teammate_redirects_for_fellow_agent_name(self):
        """"Milo" isn't a human teammate -- it's the Insights Agent. The tool
        should redirect to consult_insights_agent instead of a generic
        not-found error, since the notify lookup can never succeed for it."""
        tools = self._tools()
        result = tools["notify_teammate"]("Milo", "Heads up", "msg")
        self.assertIn("fellow AI agent", result)
        self.assertIn("consult_insights_agent", result)
        self.assertFalse(Notification.objects.filter(notification_type='ai_report', title="Heads up").exists())

    def test_assign_task_redirects_for_fellow_agent_name(self):
        """assign_task's redirect points at assign_task_to_agent (the real
        mechanism), not consult_* -- consulting is for questions, this is
        for handing off task ownership."""
        tools = self._tools()
        result = tools["assign_task"](str(self.task.id), "Nora")
        self.assertIn("fellow AI agent", result)
        self.assertIn("assign_task_to_agent", result)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_to)

    def test_notify_teammate_prefers_real_human_over_agent_name_collision(self):
        """If a real teammate happens to be named after an agent, the
        genuine WorkspaceMembership lookup must win -- the hint is a
        fallback for lookup failures only, never a hard block."""
        human_milo = User.objects.create_user(
            username="milo", password="pass1234", email="milo@test.com", first_name="Milo"
        )
        WorkspaceMembership.objects.create(workspace=self.workspace, user=human_milo, is_active=True, role="contributor")
        tools = self._tools()
        result = tools["notify_teammate"]("Milo", "Heads up", "msg")
        self.assertIn("Notified", result)
        self.assertTrue(Notification.objects.filter(recipient=human_milo, title="Heads up").exists())

    @mock.patch("gtm.utils_async.run_in_background", side_effect=lambda target, *args, **kwargs: target(*args))
    @mock.patch("gtm.action_item_completion.complete_action_item")
    def test_assign_task_to_agent_routes_and_triggers_completion(self, mock_complete, mock_run_bg):
        """run_in_background is patched to run synchronously so the effect
        is observable immediately. complete_action_item is mocked as a black
        box (same convention as dashboard.tests_smoke's
        ActionItemAICompletionTests) -- it owns the actual status/comment
        write-back, so what's observable here is _complete_action_item_background's
        own side effects: the cached completion state and the activity log,
        confirmed via the polling cache key it writes to."""
        from django.core.cache import cache
        from dashboard.views.actions import _action_item_complete_status_cache_key

        mock_complete.return_value = {
            "success": True, "status": "done",
            "summary": "Drafted the ICP one-pager.", "document_id": None,
        }
        self.task.session = self.session
        self.task.save()
        tools = self._tools()
        result = tools["assign_task_to_agent"](str(self.task.id), "insights")
        self.assertIn("Routed", result)
        self.assertIn("Milo", result)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assigned_agent_type, "insights")
        mock_complete.assert_called_once()
        cached_status = cache.get(_action_item_complete_status_cache_key(self.task.id))
        self.assertEqual(cached_status["state"], "done")
        self.assertEqual(cached_status["status"], "done")

    def test_assign_task_to_agent_rejects_invalid_agent_type(self):
        tools = self._tools()
        result = tools["assign_task_to_agent"](str(self.task.id), "not_a_real_agent")
        self.assertIn("isn't a recognized agent type", result)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_agent_type)

    def test_assign_task_to_agent_respects_can_assign_tasks_permission(self):
        tools = self._tools(user=self.teammate)  # contributor -- can_assign_tasks is False
        result = tools["assign_task_to_agent"](str(self.task.id), "insights")
        self.assertIn("don't have permission", result)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.assigned_agent_type)

    def test_assign_task_to_agent_labels_only_without_linked_session(self):
        """A task with no linked assessment can still be routed/labeled, but
        honestly can't be attempted -- no background job should fire."""
        unlinked_task = ActionItem.objects.create(
            workspace=self.workspace, note="No session here", status="todo", session=None
        )
        tools = self._tools()
        with mock.patch("gtm.utils_async.run_in_background") as mock_run_bg:
            result = tools["assign_task_to_agent"](str(unlinked_task.id), "insights")
        self.assertIn("Routed", result)
        self.assertIn("no linked assessment", result)
        unlinked_task.refresh_from_db()
        self.assertEqual(unlinked_task.assigned_agent_type, "insights")
        mock_run_bg.assert_not_called()

    def test_assign_task_to_agent_labels_only_when_not_todo(self):
        self.task.session = self.session
        self.task.status = "done"
        self.task.save()
        tools = self._tools()
        with mock.patch("gtm.utils_async.run_in_background") as mock_run_bg:
            result = tools["assign_task_to_agent"](str(self.task.id), "insights")
        self.assertIn("Routed", result)
        self.assertIn("already", result)
        mock_run_bg.assert_not_called()
        self.task.status = "todo"
        self.task.save()

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

    def test_lookup_crm_company_without_connection_returns_setup_hint(self):
        tools = self._tools()
        with mock.patch("gtm.integrations.hubspot_client.get_client_for_workspace", return_value=None):
            result = tools["lookup_crm_company"]("acme.com")
        self.assertIn("doesn't have HubSpot connected", result)

    def test_lookup_crm_company_formats_found_company(self):
        tools = self._tools()
        fake_client = mock.Mock()
        fake_client.find_company.return_value = {
            "id": "123",
            "properties": {"name": "Acme Inc", "domain": "acme.com", "industry": "Software"},
        }
        with mock.patch("gtm.integrations.hubspot_client.get_client_for_workspace", return_value=fake_client):
            result = tools["lookup_crm_company"]("acme.com")
        self.assertIn("Acme Inc", result)
        self.assertIn("Software", result)
        fake_client.find_company.assert_called_once_with("acme.com")

    def test_lookup_crm_company_ambiguous_match_asks_for_specificity(self):
        tools = self._tools()
        fake_client = mock.Mock()
        fake_client.find_company.return_value = None
        with mock.patch("gtm.integrations.hubspot_client.get_client_for_workspace", return_value=fake_client):
            result = tools["lookup_crm_company"]("Acme")
        self.assertIn("couldn't find a single HubSpot company", result)

    def test_log_insight_to_crm_creates_note_on_matched_company(self):
        tools = self._tools()
        fake_client = mock.Mock()
        fake_client.find_company.return_value = {"id": "123", "properties": {"name": "Acme Inc"}}
        with mock.patch("gtm.integrations.hubspot_client.get_client_for_workspace", return_value=fake_client):
            result = tools["log_insight_to_crm"]("acme.com", "Weak ICP definition detected.")
        self.assertIn("Logged a note", result)
        self.assertIn("Acme Inc", result)
        fake_client.create_note.assert_called_once_with("123", "[ForgeGTM AI] Weak ICP definition detected.")

    def test_log_insight_to_crm_without_connection_returns_setup_hint(self):
        tools = self._tools()
        with mock.patch("gtm.integrations.hubspot_client.get_client_for_workspace", return_value=None):
            result = tools["log_insight_to_crm"]("acme.com", "Some finding.")
        self.assertIn("doesn't have HubSpot connected", result)

    def test_create_crm_follow_up_task_respects_can_assign_tasks_permission(self):
        tools = self._tools(user=self.teammate)  # contributor -- can_assign_tasks is False
        with mock.patch("gtm.integrations.hubspot_client.get_client_for_workspace") as mock_get_client:
            result = tools["create_crm_follow_up_task"](str(self.task.id), "acme.com")
        self.assertIn("don't have permission", result)
        mock_get_client.assert_not_called()

    def test_create_crm_follow_up_task_creates_then_updates_idempotently(self):
        tools = self._tools()  # actor is admin
        fake_client = mock.Mock()
        fake_client.find_company.return_value = {"id": "123", "properties": {"name": "Acme Inc"}}
        fake_client.create_task.return_value = "task-999"

        with mock.patch("gtm.integrations.hubspot_client.get_client_for_workspace", return_value=fake_client):
            first = tools["create_crm_follow_up_task"](str(self.task.id), "acme.com")

        self.assertIn("Pushed", first)
        self.assertIn("Acme Inc", first)
        fake_client.create_task.assert_called_once()
        fake_client.update_task.assert_not_called()
        self.task.refresh_from_db()
        self.assertEqual(self.task.external_crm_task_id, "task-999")
        self.assertEqual(self.task.crm_sync_status, "synced")
        self.assertIsNotNone(self.task.crm_synced_at)

        with mock.patch("gtm.integrations.hubspot_client.get_client_for_workspace", return_value=fake_client):
            second = tools["create_crm_follow_up_task"](str(self.task.id), "acme.com")

        self.assertIn("Updated", second)
        fake_client.update_task.assert_called_once_with("task-999", mock.ANY, mock.ANY, None)
        fake_client.create_task.assert_called_once()  # still only called once total
