"""
Tests for the Tasks board stat strip (dashboard/views/helpers.py::
build_task_board_context) and the completed_at bookkeeping it relies on
(gtm/models.py::ActionItem.save).
"""

import datetime

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.views.helpers import build_task_board_context
from gtm.models import ActionItem
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


class ActionItemCompletedAtTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Completed At Co")

    def test_marking_done_stamps_completed_at(self):
        item = ActionItem.objects.create(workspace=self.workspace, note="Ship it")
        self.assertIsNone(item.completed_at)
        item.status = "done"
        item.save()
        self.assertIsNotNone(item.completed_at)

    def test_resaving_done_task_keeps_original_completion_time(self):
        item = ActionItem.objects.create(workspace=self.workspace, note="Ship it", status="done")
        original = item.completed_at
        item.note = "Ship it today"
        item.save()
        self.assertEqual(item.completed_at, original)

    def test_moving_out_of_done_clears_completed_at(self):
        item = ActionItem.objects.create(workspace=self.workspace, note="Ship it", status="done")
        item.status = "todo"
        item.save()
        item.refresh_from_db()
        self.assertIsNone(item.completed_at)

    def test_status_only_update_fields_still_persists_completed_at(self):
        # The playbook toggle saves with update_fields=["status"].
        item = ActionItem.objects.create(workspace=self.workspace, note="Ship it")
        item.status = "done"
        item.save(update_fields=["status"])
        item.refresh_from_db()
        self.assertIsNotNone(item.completed_at)


class BuildTaskBoardContextTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Board Stats Co")
        self.user = User.objects.create_user(username="board-stats", password="pass1234")
        self.today = timezone.localdate()
        self.week_end = self.today - datetime.timedelta(days=self.today.weekday()) + datetime.timedelta(days=6)

    def _item(self, note, **kwargs):
        return ActionItem.objects.create(workspace=self.workspace, note=note, **kwargs)

    def _context(self):
        return build_task_board_context(ActionItem.objects.filter(workspace=self.workspace))

    def test_stats_and_flags(self):
        overdue = self._item("Overdue", due_date=self.today - datetime.timedelta(days=3), assigned_to=self.user)
        due_week = self._item("Due this week", due_date=self.week_end, assigned_to=self.user)
        self._item("Later", due_date=self.week_end + datetime.timedelta(days=1), assigned_to=self.user)
        unassigned = self._item("Nobody owns this")
        self._item("Routed to an agent", assigned_agent_type="portfolio")
        done_now = self._item("Finished", status="done")
        done_before = self._item("Finished earlier", status="done")
        ActionItem.objects.filter(pk=done_before.pk).update(
            completed_at=timezone.now() - datetime.timedelta(days=8),
        )

        context = self._context()
        self.assertEqual(context["task_stats"], {
            "overdue": 1, "due_week": 1, "unassigned": 1, "done_week": 1,
        })
        flags = {item.note: item.board_flags for col in ("top_todo", "top_doing", "top_done") for item in context[col]}
        self.assertEqual(flags[overdue.note], "overdue")
        self.assertEqual(flags[due_week.note], "due_week")
        self.assertEqual(flags[unassigned.note], "unassigned")
        self.assertEqual(flags[done_now.note], "done_week")
        self.assertEqual(flags[done_before.note], "")
        self.assertEqual(flags["Routed to an agent"], "")

    def test_done_task_is_never_overdue(self):
        self._item("Late but done", status="done", due_date=self.today - datetime.timedelta(days=30))
        self.assertEqual(self._context()["task_stats"]["overdue"], 0)


@override_settings(SECURE_SSL_REDIRECT=False)
class TasksBoardRenderingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="board-render", password="pass1234")
        self.workspace = Workspace.objects.create(name="Board Render Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.user, role="admin", is_active=True)
        self.item = ActionItem.objects.create(
            workspace=self.workspace, note="Past due task",
            due_date=timezone.localdate() - datetime.timedelta(days=2),
        )
        self.client.force_login(self.user)

    def test_tasks_page_shows_strip_and_overdue_badge(self):
        response = self.client.get(reverse("tasks_board"), {"workspace": str(self.workspace.id)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="tasks-board"')
        self.assertContains(response, 'data-task-filter-btn="overdue"')
        self.assertContains(response, 'data-board-flags="overdue unassigned"')
        self.assertContains(response, ">Overdue</span>", count=2)  # strip label + card badge

    def test_card_actions_sit_in_footer_not_over_title(self):
        response = self.client.get(reverse("tasks_board"), {"workspace": str(self.workspace.id)})
        self.assertContains(response, 'aria-label="Edit task"')
        self.assertContains(response, 'aria-label="Delete task"')
        # The old hover-only toolbar floated over the title and was unreachable on touch screens.
        self.assertNotContains(response, "absolute top-2 right-2 opacity-0")

    def test_board_refresh_keeps_strip(self):
        response = self.client.get(reverse("refresh_action_items"), {"workspace": str(self.workspace.id)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-task-filter-btn="overdue"')
        self.assertContains(response, 'data-board-flags="overdue unassigned"')
