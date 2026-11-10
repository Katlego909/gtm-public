"""
Cross-worker coordination for the workspace insights digest and the AI
spawn guards.

Django's cache is per-process here (CACHES is unset -> LocMemCache), and Cloud
Run runs several gunicorn workers. State that a *different* worker must read
-- "is the digest still generating?", "has a thread already been spawned for
this?" -- has to live in the DB-backed lock table (gtm/utils_locks.py).
Each test clears the cache between steps to simulate the next request landing
on another worker.
"""
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm import utils_locks
from gtm.models import AssessmentSession, ResultSnapshot, WorkspaceChatMessage
from gtm.models_workspace import Workspace, WorkspaceMembership
from gtm.services import _kickoff_playbook_generation

from dashboard.views import workspace_agent_api as api

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class InsightsDigestStatusTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="insights-status-user", password="pass1234", email="isu@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Insights Status Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.qs = f"?workspace={self.workspace.id}"

    def _status(self):
        cache.clear()  # the poll lands on a worker that never saw the refresh
        response = self.client.get(reverse("dashboard_agent_insights_status") + self.qs)
        self.assertEqual(response.status_code, 200)
        return response.json()["status"]

    def _refresh(self):
        """POST a refresh, capturing (not running) the background job."""
        with mock.patch.object(api, "run_in_background") as spawn:
            response = self.client.post(reverse("dashboard_agent_insights_refresh") + self.qs)
        self.assertEqual(response.status_code, 200)
        return spawn

    def _run_job(self, spawn, **patches):
        job, *args = spawn.call_args.args
        with mock.patch.object(api, "send_notification"), \
             mock.patch.object(api, "build_insights_context", return_value={}), \
             mock.patch.object(api, "format_insights_digest_text", **patches):
            job(*args)

    def test_idle_before_any_digest(self):
        self.assertEqual(self._status(), "idle")

    def test_generating_is_visible_to_other_workers_while_job_runs(self):
        self._refresh()
        self.assertEqual(self._status(), "generating")

    def test_done_after_job_completes(self):
        spawn = self._refresh()
        self._run_job(spawn, return_value="Digest body")
        self.assertEqual(self._status(), "done")
        self.assertTrue(
            WorkspaceChatMessage.objects.filter(
                workspace=self.workspace, intent="insights_digest"
            ).exists()
        )

    def test_failure_is_visible_to_other_workers_and_cleared_by_retry(self):
        spawn = self._refresh()
        with mock.patch.object(api, "log_ai_error"):
            self._run_job(spawn, side_effect=RuntimeError("boom"))
        self.assertEqual(self._status(), "failed")

        self._refresh()
        self.assertEqual(self._status(), "generating")


class SpawnGuardAcrossWorkersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="spawn-guard-user", password="pass1234", email="sgu@test.com"
        )
        session = AssessmentSession.objects.create(
            owner_client_id="spawn-guard-client", user=cls.user, is_completed=True,
        )
        cls.snapshot = ResultSnapshot.objects.create(session=session, overall=50, category_breakdown=[])

    def test_playbook_kickoff_spawns_once_across_workers(self):
        allowed = SimpleNamespace(allowed=True)
        with mock.patch("gtm.ai_credits.can_spend", return_value=allowed), \
             mock.patch("gtm.ai_credits.resolve_account_for_session", return_value=None), \
             mock.patch("gtm.services.run_in_background") as spawn:
            first = _kickoff_playbook_generation(self.snapshot)
            cache.clear()  # second poll handled by another worker
            second = _kickoff_playbook_generation(self.snapshot)
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(spawn.call_count, 1)

    def test_spawn_cooldown_expires(self):
        key = f"gtm:playbook:kickoff:{self.snapshot.id}"
        self.assertTrue(utils_locks.acquire(key, 20))
        self.assertFalse(utils_locks.acquire(key, 20))
        utils_locks.release(key)
        self.assertTrue(utils_locks.acquire(key, 20))
