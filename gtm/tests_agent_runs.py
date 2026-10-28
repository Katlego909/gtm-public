"""
Agent runs (gtm/agent_runs.py): plan first, a person approves, then the
agent acts -- with every write tool gated against the approved plan.

Gemini is replaced by a scripted fake chat: each send_message pops the next
script function, which calls the agent's real tools by name, so the gate,
pausing, and write-back are exercised exactly as in production. Background
threads run synchronously.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from dashboard.models import Notification
from gtm import agent_runs
from gtm.agent_runs import RunDecisionError, answer_question, approve_run, reject_run, retry_run, start_run
from gtm.models import ActionItem, ActionItemComment, AgentRun, AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


class _Response:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = None


class _Chat:
    def __init__(self, client, config):
        self.client, self.config = client, config
        self.tools = {t.__name__: t for t in config.tools}

    def send_message(self, message):
        script = self.client.scripts.pop(0) if self.client.scripts else (lambda tools, config: "")
        return _Response(script(self.tools, self.config) or "")


class _FakeClient:
    """Stands in for the Vertex AI client; records every chat's config."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.configs = []
        self.chats = self

    def create(self, model, config, history):
        self.configs.append(config)
        return _Chat(self, config)


def _propose(*steps, summary="Plan summary."):
    def script(tools, config):
        return tools["propose_plan"](summary=summary, steps=[dict(kind=k, title=t, detail=f"{t} detail") for k, t in steps])
    return script


class AgentRunTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="run-admin", password="pass1234")
        cls.teammate = User.objects.create_user(username="priya", password="pass1234", first_name="Priya")
        cls.workspace = Workspace.objects.create(name="Runs Co")
        WorkspaceMembership.objects.create(workspace=cls.workspace, user=cls.admin, role="admin", is_active=True)
        WorkspaceMembership.objects.create(workspace=cls.workspace, user=cls.teammate, role="contributor", is_active=True)
        cls.session = AssessmentSession.objects.create(
            owner_client_id="runs-client", user=cls.admin, workspace=cls.workspace, company_name="Runs Co",
        )

    def setUp(self):
        self.item = ActionItem.objects.create(
            session=self.session, workspace=self.workspace, note="Define our ICP", status="todo", created_by=self.admin,
        )
        patcher = mock.patch.object(agent_runs, "run_in_background", side_effect=lambda target, *a, **k: target(*a))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _use(self, *scripts):
        self.client = _FakeClient(scripts)
        patcher = mock.patch.object(agent_runs, "_client_or_error", return_value=(self.client, None, ""))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _start(self):
        return start_run(agent_type="gtm_strategist", goal=self.item.note, user=self.admin, action_item=self.item)


class PlanningTests(AgentRunTestBase):
    def test_plan_waits_for_approval_and_notifies_reviewer(self):
        self._use(_propose(("create_document", "ICP one-pager"), ("create_subtasks", "Validation tasks")))
        run = self._start()
        run.refresh_from_db()

        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual([s["kind"] for s in run.plan], ["create_document", "create_subtasks"])
        self.assertTrue(all(s["status"] == "pending" for s in run.plan))
        self.assertTrue(Notification.objects.filter(recipient=self.admin, notification_type="agent_plan").exists())
        self.item.refresh_from_db()
        self.assertEqual(self.item.current_run_id, run.id)
        self.assertEqual(self.item.status, "todo")

    def test_planning_turn_has_no_write_tools(self):
        self._use(_propose(("create_document", "Doc")))
        self._start()
        names = {t.__name__ for t in self.client.configs[0].tools}
        self.assertIn("propose_plan", names)
        self.assertIn("get_gtm_diagnosis", names)
        for write_tool in agent_runs.TOOL_KIND:
            self.assertNotIn(write_tool, names)

    def test_unknown_step_kind_is_rejected(self):
        def script(tools, config):
            self.result = tools["propose_plan"](summary="x", steps=[{"kind": "delete_everything", "title": "x"}])
        self._use(script)
        run = self._start()
        run.refresh_from_db()
        self.assertIn("Unknown or unavailable", self.result)
        self.assertEqual(run.status, "failed")

    def test_crm_steps_are_unavailable_without_a_connected_crm(self):
        def script(tools, config):
            self.result = tools["propose_plan"](summary="x", steps=[{"kind": "crm_update", "title": "Log to CRM"}])
            self.instruction = config.system_instruction
        self._use(script)
        self._start()
        self.assertIn("unavailable", self.result)
        self.assertNotIn("- crm_update:", self.instruction)

    def test_no_plan_fails_after_nudge(self):
        self._use(lambda tools, config: "I think you should do X.", lambda tools, config: "Still no plan.")
        run = self._start()
        run.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(run.status, "failed")
        self.assertEqual(self.item.ai_run_state, "failed")

    def test_second_start_while_waiting_for_approval_is_refused(self):
        from dashboard.views.actions import start_ai_completion

        self._use(_propose(("create_document", "Doc")))
        self._start()
        self.item.refresh_from_db()
        self.assertFalse(start_ai_completion(self.item, self.admin.id, self.workspace.id))


class ExecutionTests(AgentRunTestBase):
    def _planned(self, *steps):
        self._use(_propose(*steps))
        run = self._start()
        run.refresh_from_db()
        return run

    def test_approved_plan_runs_and_task_is_done_with_real_outputs(self):
        run = self._planned(("create_document", "ICP one-pager"), ("create_subtasks", "Validation tasks"))

        def execute(tools, config):
            tools["create_document"](title="ICP one-pager", content="Inclusion: B2B SaaS, 50-500 staff.")
            tools["create_subtasks"](tasks=[
                {"note": "Interview 5 best customers", "assignee": "Priya", "due_date": "2026-11-15"},
                {"note": "Pick a sales deck for the ICP", "assignee": "Theo"},
            ])
            return "Wrote the ICP one-pager and created two follow-up tasks."
        self.client.scripts.append(execute)

        approve_run(run, self.admin, ["s1", "s2"])
        run.refresh_from_db()
        self.item.refresh_from_db()

        self.assertEqual(run.status, "completed")
        self.assertEqual([s["status"] for s in run.plan], ["done", "done"])
        self.assertEqual(self.item.status, "done")
        self.assertIsNotNone(self.item.deliverable_document)
        subtasks = {t.note: t for t in self.item.subtasks.all()}
        self.assertEqual(subtasks["Interview 5 best customers"].assigned_to, self.teammate)
        self.assertEqual(str(subtasks["Interview 5 best customers"].due_date), "2026-11-15")
        self.assertEqual(subtasks["Pick a sales deck for the ICP"].assigned_agent_type, "resource")
        comment = ActionItemComment.objects.get(action_item=self.item)
        self.assertEqual(comment.agent_type, "gtm_strategist")

    def test_gate_refuses_a_write_the_plan_did_not_approve(self):
        run = self._planned(("create_document", "Doc"))

        def execute(tools, config):
            self.refusal = tools["notify_teammate"](username="Priya", title="Hi", message="Hello")
            return ""
        self.client.scripts.append(execute)

        approve_run(run, self.admin, ["s1"])
        run.refresh_from_db()
        self.item.refresh_from_db()

        self.assertIn("Refused", self.refusal)
        self.assertFalse(Notification.objects.filter(recipient=self.teammate, title="Hi").exists())
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.plan[0]["status"], "failed")
        self.assertEqual(self.item.status, "doing")
        self.assertIn("Not done yet", ActionItemComment.objects.get(action_item=self.item).text)

    def test_unticked_step_is_skipped_and_its_tool_refused(self):
        run = self._planned(("create_document", "Doc"), ("create_subtasks", "Tasks"))

        def execute(tools, config):
            self.refusal = tools["create_subtasks"](tasks=[{"note": "Should not exist"}])
            tools["create_document"](title="Doc", content="Real content.")
            return "Done."
        self.client.scripts.append(execute)

        approve_run(run, self.admin, ["s1"])
        run.refresh_from_db()

        self.assertIn("Refused", self.refusal)
        self.assertFalse(ActionItem.objects.filter(note="Should not exist").exists())
        self.assertEqual([s["status"] for s in run.plan], ["done", "skipped"])
        self.assertEqual(run.status, "completed")

    def test_a_tool_that_changes_nothing_does_not_complete_its_step(self):
        run = self._planned(("assign_task", "Assign to Priya"))

        def execute(tools, config):
            tools["assign_task"](action_item_id="999999", assignee="Priya")
            return ""
        self.client.scripts.append(execute)

        approve_run(run, self.admin, ["s1"])
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

    def test_ask_teammate_pauses_and_answer_resumes(self):
        run = self._planned(("ask_teammate", "Ask Priya for churn data"), ("create_document", "Churn brief"))

        def ask(tools, config):
            tools["ask_teammate"](teammate="Priya", question="What was Q3 logo churn?")
            self.blocked = tools["create_document"](title="Too early", content="x")
            return "Waiting on Priya."
        self.client.scripts.append(ask)

        approve_run(run, self.admin, ["s1", "s2"])
        run.refresh_from_db()
        self.assertEqual(run.status, "waiting_for_input")
        self.assertIn("waiting", self.blocked)
        question = run.questions.get()
        self.assertTrue(Notification.objects.filter(recipient=self.teammate, notification_type="agent_question").exists())

        def resume(tools, config):
            self.resume_instruction = config.system_instruction
            tools["create_document"](title="Churn brief", content="Q3 logo churn was 4%.")
            return "Wrote the churn brief."
        self.client.scripts.append(resume)

        answer_question(question, self.teammate, "4% logo churn in Q3.")
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertIn("4% logo churn in Q3.", self.resume_instruction)

    def test_retry_runs_only_the_steps_still_not_done(self):
        run = self._planned(("create_document", "Doc"), ("create_subtasks", "Tasks"))
        self.client.scripts.append(lambda tools, config: tools["create_document"](title="Doc", content="x") and "")
        approve_run(run, self.admin, ["s1", "s2"])
        run.refresh_from_db()
        self.assertEqual(run.status, "failed")

        def finish(tools, config):
            tools["create_subtasks"](tasks=[{"note": "Follow-up"}])
            return "Done."
        self.client.scripts[:] = [finish]
        retry_run(run, self.admin)
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")
        self.assertEqual(ActionItem.objects.filter(parent=self.item).count(), 1)


class DecisionTests(AgentRunTestBase):
    def setUp(self):
        super().setUp()
        self._use(_propose(("create_document", "Doc")))
        self.run = self._start()
        self.run.refresh_from_db()

    def test_contributor_cannot_approve(self):
        with self.assertRaises(RunDecisionError):
            approve_run(self.run, self.teammate, ["s1"])

    def test_approving_with_no_steps_is_refused(self):
        with self.assertRaises(RunDecisionError):
            approve_run(self.run, self.admin, [])

    def test_reject_with_feedback_replans_with_that_feedback(self):
        self.client.scripts.append(_propose(("create_subtasks", "Tasks instead")))
        reject_run(self.run, self.admin, "Make tasks, not a document.", revise=True)
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "awaiting_approval")
        self.assertEqual(self.run.plan[0]["kind"], "create_subtasks")
        self.assertIn("Make tasks, not a document.", self.client.configs[-1].system_instruction)

    def test_dismiss_ends_the_run(self):
        reject_run(self.run, self.admin)
        self.run.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(self.run.status, "rejected")
        self.assertEqual(self.item.ai_run_state, "")


class RunPanelViewTests(AgentRunTestBase):
    def setUp(self):
        super().setUp()
        self._use(_propose(("create_document", "ICP one-pager")))
        self.run = self._start()

    def test_member_sees_the_plan_and_outsider_gets_404(self):
        self.client_http = self.client_class()
        self.client_http.force_login(self.admin)
        response = self.client_http.get(reverse("agent_run_panel", args=[self.run.id]))
        self.assertContains(response, "ICP one-pager")
        self.assertContains(response, "Approve and run")

        outsider = User.objects.create_user(username="outsider", password="pass1234")
        self.client_http.force_login(outsider)
        self.assertEqual(self.client_http.get(reverse("agent_run_panel", args=[self.run.id])).status_code, 404)

    def test_contributor_sees_the_plan_without_approve_controls(self):
        http = self.client_class()
        http.force_login(self.teammate)
        response = http.get(reverse("agent_run_panel", args=[self.run.id]))
        self.assertContains(response, "ICP one-pager")
        self.assertNotContains(response, "Approve and run")
        response = http.post(reverse("agent_run_approve", args=[self.run.id]), {"keep_step": ["s1"]})
        self.assertContains(response, "permission")

    def test_approve_through_the_view(self):
        self.client.scripts.append(lambda tools, config: tools["create_document"](title="ICP", content="x") and "Done.")
        http = self.client_class()
        http.force_login(self.admin)
        response = http.post(reverse("agent_run_approve", args=[self.run.id]), {"keep_step": ["s1"]})
        self.assertEqual(response["HX-Trigger"], "agentRunChanged")
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, "completed")

    def test_board_card_offers_plan_review(self):
        http = self.client_class()
        http.force_login(self.admin)
        response = http.get(reverse("tasks_board") + f"?workspace={self.workspace.id}")
        self.assertContains(response, "proposed a 1-step plan")
        self.assertContains(response, reverse("agent_run_panel", args=[self.run.id]))
