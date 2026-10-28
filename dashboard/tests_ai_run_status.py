"""
Tests for the "Complete with AI" run status: it's stored on the ActionItem
(not Django's per-process cache), so any worker can answer a poll, and the
board renders running / stale / failed states from the server.
"""

import datetime
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.views.actions import start_ai_completion
from dashboard.views.helpers import action_item_ai_lock_key
from gtm.ai_services import _acquire_lock, _release_lock
from gtm.models import ActionItem, AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class AIRunStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ai-run", password="pass1234")
        self.workspace = Workspace.objects.create(name="AI Run Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.user, role="admin", is_active=True)
        self.session = AssessmentSession.objects.create(
            owner_client_id="ai-run-client", user=self.user, workspace=self.workspace,
            company_name="AI Run Co", is_completed=True,
        )
        self.item = ActionItem.objects.create(
            workspace=self.workspace, session=self.session, note="Enforce an ICP-fit field",
        )
        self.client.force_login(self.user)
        self.qs = {"workspace": str(self.workspace.id)}

    def _hold_lock(self):
        lock_key = action_item_ai_lock_key(self.item.id)
        self.assertTrue(_acquire_lock(lock_key, ttl_seconds=60))
        self.addCleanup(_release_lock, lock_key)

    def _mark_running(self, seconds_ago=42):
        ActionItem.objects.filter(pk=self.item.pk).update(
            ai_run_state="running",
            ai_run_started_at=timezone.now() - datetime.timedelta(seconds=seconds_ago),
        )

    def _status(self):
        response = self.client.get(reverse("complete_action_item_ai_status", args=[self.item.id]), self.qs)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _board(self):
        response = self.client.get(reverse("tasks_board"), self.qs)
        self.assertEqual(response.status_code, 200)
        return response

    def test_start_records_running_without_touching_edited_date(self):
        before = ActionItem.objects.get(pk=self.item.pk).updated_at
        with mock.patch("gtm.agent_runs.run_in_background"):
            self.assertTrue(start_ai_completion(self.item, self.user.id, self.workspace.id))
            self.addCleanup(_release_lock, action_item_ai_lock_key(self.item.id))
            # A second start while the first holds the lock is refused.
            self.assertFalse(start_ai_completion(self.item, self.user.id, self.workspace.id))
        self.item.refresh_from_db()
        self.assertEqual(self.item.ai_run_state, "running")
        self.assertIsNotNone(self.item.ai_run_started_at)
        self.assertEqual(self.item.updated_at, before)

    def test_running_with_live_lock_reads_running(self):
        self._mark_running()
        self._hold_lock()
        self.assertEqual(self._status()["state"], "running")

    def test_running_without_lock_reads_stale(self):
        self._mark_running(seconds_ago=400)
        self.assertEqual(self._status()["state"], "stale")

    def _start_synchronously(self):
        with mock.patch("gtm.agent_runs.run_in_background", side_effect=lambda target, *a, **k: target(*a)):
            start_ai_completion(self.item, self.user.id, self.workspace.id)

    def test_failed_run_records_error(self):
        with mock.patch("gtm.agent_runs._client_or_error", return_value=(None, None, "Out of AI credits.")):
            self._start_synchronously()
        data = self._status()
        self.assertEqual(data["state"], "failed")
        self.assertEqual(data["error"], "Out of AI credits.")

    def test_crashed_run_records_failure(self):
        with mock.patch("gtm.agent_runs._plan", side_effect=RuntimeError("boom")):
            self._start_synchronously()
        self.item.refresh_from_db()
        self.assertEqual(self.item.ai_run_state, "failed")
        self.assertEqual(self.item.ai_run_error, "Ran into a technical error. You can retry this run.")

    def test_board_shows_running_card_locked(self):
        self._mark_running()
        self._hold_lock()
        response = self._board()
        self.assertContains(response, "</span> is working on this")
        self.assertContains(response, '<span class="font-semibold">Charlie</span>')
        self.assertContains(response, f'data-ai-running="{self.item.id}"')
        self.assertContains(response, "data-ai-locked>")
        self.assertNotContains(response, "AI is working on this...")

    def test_board_offers_retry_when_stale(self):
        self._mark_running(seconds_ago=400)
        response = self._board()
        self.assertContains(response, "This is taking longer than expected.")
        self.assertContains(response, "Try again")
        self.assertNotContains(response, "data-ai-locked>")

    def test_board_shows_failure_reason(self):
        ActionItem.objects.filter(pk=self.item.pk).update(ai_run_state="failed", ai_run_error="Out of AI credits.")
        response = self._board()
        self.assertContains(response, "Charlie couldn't finish: Out of AI credits.")
        self.assertContains(response, "Try again")
