"""
Regression tests for the task-ID-hallucination bug: agents need the real
ActionItem.id exposed in every task-listing tool's output, or they invent a
plausible-looking one when a later tool call (assign_task, comment_on_task,
assign_task_to_agent) needs a real ID.

Also covers the follow-up fix: real IDs are tracked structurally
(WorkspaceChatMessage/ChatMessage.task_refs) rather than only existing as
`[ID: n]` substrings in text the model may or may not repeat back to the
user -- see agent_runtime.record_task_ref/build_task_context_prompt/
strip_task_id_brackets and gtm/agent_actions.py's find_tasks tool.
"""

import datetime
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from gtm.agent_actions import build_agent_action_tools
from gtm.agent_runtime import record_task_ref, strip_task_id_brackets
from gtm.ai_chat import _build_chat_history, _build_session_tools
from gtm.models import ActionItem, AssessmentSession, ChatMessage, WorkspaceChatMessage
from gtm.models_workspace import Workspace, WorkspaceMembership
from gtm.workspace_agent_chat import (
    _build_workspace_chat_history,
    _build_workspace_tools,
    build_workspace_agent_context,
    handle_overdue_tasks,
)

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

    # ------------------------------------------------------------------
    # find_tasks
    # ------------------------------------------------------------------

    def test_find_tasks_exposes_real_id_by_text_query(self):
        tools = {t.__name__: t for t in build_agent_action_tools(self.workspace, self.user)}
        result = tools["find_tasks"](query="ICP inclusion")
        self.assertIn(f"[ID: {self.task.id}]", result)

    def test_find_tasks_excludes_done_by_default(self):
        done_task = ActionItem.objects.create(
            session=self.session, workspace=self.workspace,
            note="Already done thing", status="done",
        )
        tools = {t.__name__: t for t in build_agent_action_tools(self.workspace, self.user)}
        result = tools["find_tasks"]()
        self.assertIn(f"[ID: {self.task.id}]", result)
        self.assertNotIn(f"[ID: {done_task.id}]", result)

    def test_find_tasks_filters_by_status(self):
        done_task = ActionItem.objects.create(
            session=self.session, workspace=self.workspace,
            note="Wrap up onboarding", status="done",
        )
        tools = {t.__name__: t for t in build_agent_action_tools(self.workspace, self.user)}
        result = tools["find_tasks"](status="done")
        self.assertIn(f"[ID: {done_task.id}]", result)
        self.assertNotIn(f"[ID: {self.task.id}]", result)

    def test_find_tasks_filters_by_created_on(self):
        dated_task = ActionItem.objects.create(
            session=self.session, workspace=self.workspace,
            note="July planning task", status="todo",
        )
        ActionItem.objects.filter(pk=dated_task.pk).update(
            created_at=timezone.make_aware(datetime.datetime(2024, 3, 10, 12, 0))
        )
        tools = {t.__name__: t for t in build_agent_action_tools(self.workspace, self.user)}
        result = tools["find_tasks"](created_on="2024-03-10")
        self.assertIn(f"[ID: {dated_task.id}]", result)
        self.assertNotIn(f"[ID: {self.task.id}]", result)

    def test_find_tasks_no_match_returns_friendly_message(self):
        tools = {t.__name__: t for t in build_agent_action_tools(self.workspace, self.user)}
        result = tools["find_tasks"](query="totally nonexistent query xyz")
        self.assertEqual(result, "No matching tasks found in this workspace.")

    # ------------------------------------------------------------------
    # Structured task_refs sink: tools record what they touch
    # ------------------------------------------------------------------

    def test_find_tasks_records_task_ref_in_sink(self):
        sink = []
        tools = {
            t.__name__: t
            for t in build_agent_action_tools(self.workspace, self.user, task_refs_sink=sink)
        }
        tools["find_tasks"](query="ICP inclusion")
        self.assertEqual(sink, [{"id": self.task.id, "note": self.task.note[:80]}])

    def test_comment_on_task_records_task_ref_in_sink(self):
        sink = []
        tools = {
            t.__name__: t
            for t in build_agent_action_tools(self.workspace, self.user, task_refs_sink=sink)
        }
        tools["comment_on_task"](str(self.task.id), "Checking in on this.")
        self.assertEqual(sink, [{"id": self.task.id, "note": self.task.note[:80]}])

    # ------------------------------------------------------------------
    # record_task_ref / strip_task_id_brackets (agent_runtime.py)
    # ------------------------------------------------------------------

    def test_record_task_ref_dedupes_last_seen_wins(self):
        sink = []
        record_task_ref(sink, 1, "First note")
        record_task_ref(sink, 2, "Second note")
        record_task_ref(sink, 1, "First note updated")
        self.assertEqual(
            sink,
            [{"id": 2, "note": "Second note"}, {"id": 1, "note": "First note updated"}],
        )

    def test_record_task_ref_caps_at_limit(self):
        sink = []
        for i in range(25):
            record_task_ref(sink, i, f"Task {i}", cap=20)
        self.assertEqual(len(sink), 20)
        self.assertEqual([ref["id"] for ref in sink], list(range(5, 25)))

    def test_strip_task_id_brackets_removes_leaked_id(self):
        text = "Let's work on [ID: 42] next."
        self.assertEqual(strip_task_id_brackets(text), "Let's work on next.")

    def test_strip_task_id_brackets_handles_no_brackets(self):
        text = "Nothing to strip here."
        self.assertEqual(strip_task_id_brackets(text), text)

    # ------------------------------------------------------------------
    # The key regression test: grounding survives into the next turn's
    # reconstructed history WITHOUT ever having been visible in the
    # persisted/rendered response text.
    # ------------------------------------------------------------------

    def test_workspace_chat_history_surfaces_task_refs_without_showing_ids(self):
        WorkspaceChatMessage.objects.create(
            workspace=self.workspace,
            agent_type="insights",
            user=self.user,
            message="What should we work on?",
            response="Let's focus on the ICP checklist task.",
            task_refs=[{"id": self.task.id, "note": self.task.note[:80]}],
            intent="general_chat",
        )

        history, history_task_refs = _build_workspace_chat_history(self.workspace, "insights")

        self.assertIn({"id": self.task.id, "note": self.task.note[:80]}, history_task_refs)
        for content in history:
            for part in content.parts:
                self.assertNotIn(f"[ID: {self.task.id}]", part.text or "")

    def test_session_chat_history_surfaces_task_refs_without_showing_ids(self):
        ChatMessage.objects.create(
            session=self.session,
            user=self.user,
            message="What should we work on?",
            response="Let's focus on the ICP checklist task.",
            task_refs=[{"id": self.task.id, "note": self.task.note[:80]}],
            intent="general_chat",
        )

        history, history_task_refs = _build_chat_history(self.session)

        self.assertIn({"id": self.task.id, "note": self.task.note[:80]}, history_task_refs)
        for content in history:
            for part in content.parts:
                self.assertNotIn(f"[ID: {self.task.id}]", part.text or "")

    # ------------------------------------------------------------------
    # Nested consult/handoff: a peer's grounding folds into the outer sink
    # ------------------------------------------------------------------

    def test_consult_tool_merges_nested_task_refs_into_outer_sink(self):
        outer_sink = []
        outer_document_sink = []
        with patch(
            "gtm.workspace_agent_chat.handle_general_chat_workspace",
            return_value=("Theo's answer.", [{"id": 999, "note": "Nested task"}], [
                {"id": "doc-1", "title": "Nested doc", "doc_type": "other",
                 "doc_type_display": "Other", "version": 1, "action": "created"},
            ]),
        ):
            tools = {
                t.__name__: t
                for t in _build_workspace_tools(
                    "portfolio", self.workspace, user=self.user,
                    task_refs_sink=outer_sink, document_refs_sink=outer_document_sink,
                )
            }
            result = tools["consult_resource_agent"]("What resources do we have?")

        self.assertEqual(result, "Theo's answer.")
        self.assertIn({"id": 999, "note": "Nested task"}, outer_sink)
        self.assertEqual(outer_document_sink[0]["id"], "doc-1")
