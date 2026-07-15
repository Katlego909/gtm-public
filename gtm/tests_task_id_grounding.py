"""
Regression tests for the task-ID-hallucination bug: agents need the real
ActionItem.id exposed in every task-listing tool's output, or they invent a
plausible-looking one when a later tool call (assign_task, comment_on_task,
assign_task_to_agent) needs a real ID.
"""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase

from gtm.ai_chat import _build_session_tools
from gtm.models import ActionItem, AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership
from gtm.workspace_agent_chat import build_workspace_agent_context, handle_overdue_tasks

User = get_user_model()


class TaskIdGroundingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="id-grounding", password="pass1234", email="ig@test.com"
        )
        cls.workspace = Workspace.objects.create(name="ID Grounding Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="id-grounding-client",
            user=cls.user,
            workspace=cls.workspace,
            company_name="ID Grounding Co",
        )
        cls.task = ActionItem.objects.create(
            session=cls.session, workspace=cls.workspace,
            note="Define ICP inclusion criteria", status="todo",
        )

    def test_review_current_action_items_exposes_real_id(self):
        tools = {t.__name__: t for t in _build_session_tools(self.session, user=self.user)}
        result = tools["review_current_action_items"]()
        self.assertIn(f"[ID: {self.task.id}]", result)

    def test_get_overdue_tasks_exposes_real_id(self):
        overdue_task = ActionItem.objects.create(
            session=self.session, workspace=self.workspace,
            note="Overdue thing", status="todo",
            due_date=datetime.date.today() - datetime.timedelta(days=3),
        )
        context = build_workspace_agent_context("insights", self.workspace)
        result = handle_overdue_tasks(context, "")
        self.assertIn(f"[ID: {overdue_task.id}]", result)
