"""
Test suite for the AI credits (Gemini token budget) system:
gtm/models_ai_credits.py + gtm/ai_credits.py.
"""

import threading
import time
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.db.utils import OperationalError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from unittest.mock import MagicMock, patch

from gtm.ai_credits import can_spend, record_spend, resolve_account, resolve_account_for_session
from gtm.models import AssessmentSession, ResultSnapshot
from gtm.models_ai_credits import AICreditAccount, AICreditTransaction
from gtm.models_workspace import Workspace

User = get_user_model()


class PeriodRolloverTests(TestCase):
    """AICreditAccount.current_period_bounds / remaining_tokens / roll_period_if_needed."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Rollover Co")

    def test_mid_period_account_does_not_roll(self):
        account = AICreditAccount.objects.create(
            workspace=self.workspace, token_budget=1000, tokens_used=400,
            period_started_at=timezone.now(),
        )
        remaining, _ = account.remaining_tokens()
        self.assertEqual(remaining, 600)

        account.roll_period_if_needed()
        self.assertEqual(account.tokens_used, 400)

    def test_idle_account_rolls_forward_in_one_step(self):
        """An account idle for many periods jumps straight to the current
        period instead of resetting once per elapsed period."""
        long_ago = timezone.now() - timedelta(days=40)
        account = AICreditAccount.objects.create(
            workspace=self.workspace, token_budget=1000, tokens_used=999,
            period_started_at=long_ago, period_length=AICreditAccount.PERIOD_DAILY,
        )
        remaining, reset_at = account.remaining_tokens()
        # Stale period_started_at is not "now"'s period, so remaining is
        # computed against a fresh (unused) period.
        self.assertEqual(remaining, 1000)
        self.assertGreater(reset_at, timezone.now())

        account.roll_period_if_needed()
        self.assertEqual(account.tokens_used, 0)
        self.assertGreater(account.period_started_at, long_ago)

    def test_weekly_period_bounds(self):
        account = AICreditAccount.objects.create(
            workspace=self.workspace, token_budget=1000, tokens_used=0,
            period_length=AICreditAccount.PERIOD_WEEKLY,
            period_started_at=timezone.now() - timedelta(days=3),
        )
        _start, ends_at = account.current_period_bounds()
        self.assertGreater(ends_at, timezone.now() + timedelta(days=3))


class RecordSpendTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Spend Co")
        self.account = AICreditAccount.objects.create(workspace=self.workspace, token_budget=1000)

    def test_record_spend_increments_usage_and_logs_transaction(self):
        record_spend(self.account, 150, "playbook")
        self.account.refresh_from_db()
        self.assertEqual(self.account.tokens_used, 150)
        self.assertEqual(AICreditTransaction.objects.filter(account=self.account).count(), 1)
        txn = AICreditTransaction.objects.get(account=self.account)
        self.assertEqual(txn.tokens_spent, 150)
        self.assertEqual(txn.feature, "playbook")

    def test_record_spend_noop_for_none_account(self):
        record_spend(None, 150, "playbook")  # must not raise
        self.assertEqual(AICreditTransaction.objects.count(), 0)

    def test_record_spend_noop_for_zero_tokens(self):
        record_spend(self.account, 0, "playbook")
        self.account.refresh_from_db()
        self.assertEqual(self.account.tokens_used, 0)

    def test_record_spend_rolls_period_before_debiting(self):
        self.account.period_started_at = timezone.now() - timedelta(days=5)
        self.account.tokens_used = 999
        self.account.save()

        record_spend(self.account, 100, "playbook")
        self.account.refresh_from_db()
        # Stale usage was cleared by the rollover, then the new spend applied.
        self.assertEqual(self.account.tokens_used, 100)


class AccountResolutionTests(TestCase):
    """resolve_account / resolve_account_for_session: workspace-pool-wins-over-personal-pool rule."""

    def setUp(self):
        self.user = User.objects.create_user(username="alice", password="pw")
        self.workspace = Workspace.objects.create(name="Acme")

    def test_workspace_takes_priority_over_user(self):
        AICreditAccount.objects.create(user=self.user, token_budget=100)
        account = resolve_account(workspace=self.workspace, user=self.user)
        self.assertEqual(account.workspace_id, self.workspace.id)
        self.assertIsNone(account.user_id)

    def test_personal_pool_fallback_when_no_workspace(self):
        account = resolve_account(workspace=None, user=self.user)
        self.assertEqual(account.user_id, self.user.id)
        self.assertIsNone(account.workspace_id)

    def test_none_when_no_identity(self):
        self.assertIsNone(resolve_account(workspace=None, user=None))
        anon = type("Anon", (), {"is_authenticated": False})()
        self.assertIsNone(resolve_account(workspace=None, user=anon))

    def test_get_or_create_is_idempotent(self):
        first = resolve_account(workspace=self.workspace)
        second = resolve_account(workspace=self.workspace)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(AICreditAccount.objects.filter(workspace=self.workspace).count(), 1)

    def test_resolve_account_for_session_uses_session_workspace_and_user(self):
        session = AssessmentSession.objects.create(
            owner_client_id="c1", user=self.user, workspace=self.workspace,
        )
        account = resolve_account_for_session(session)
        self.assertEqual(account.workspace_id, self.workspace.id)

    def test_resolve_account_for_session_falls_back_to_user(self):
        session = AssessmentSession.objects.create(owner_client_id="c2", user=self.user, workspace=None)
        account = resolve_account_for_session(session)
        self.assertEqual(account.user_id, self.user.id)

    def test_resolve_account_for_session_none_session(self):
        self.assertIsNone(resolve_account_for_session(None))
        account = resolve_account_for_session(None, user=self.user)
        self.assertEqual(account.user_id, self.user.id)


class CanSpendTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Budget Co")

    def test_allowed_when_under_budget(self):
        AICreditAccount.objects.create(workspace=self.workspace, token_budget=1000, tokens_used=500)
        result = can_spend(workspace=self.workspace)
        self.assertTrue(result.allowed)
        self.assertEqual(result.remaining, 500)
        self.assertEqual(result.reason, "")

    def test_blocked_when_exhausted(self):
        AICreditAccount.objects.create(workspace=self.workspace, token_budget=1000, tokens_used=1000)
        result = can_spend(workspace=self.workspace)
        self.assertFalse(result.allowed)
        self.assertEqual(result.remaining, 0)
        self.assertEqual(result.reason, "exhausted")

    def test_blocked_when_no_identity(self):
        result = can_spend(workspace=None, user=None)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "no_identity")

    def test_can_spend_creates_account_with_default_budget(self):
        result = can_spend(workspace=self.workspace)
        self.assertTrue(result.allowed)
        self.assertEqual(result.remaining, AICreditAccount.DEFAULT_WORKSPACE_TOKEN_BUDGET)


class ConcurrentDeductionTests(TransactionTestCase):
    """Proves select_for_update() in record_spend prevents lost updates
    under concurrent writers -- needs TransactionTestCase (real committed
    transactions across threads), not TestCase (wraps the test in one
    outer transaction all threads would share)."""

    def test_concurrent_record_spend_has_no_lost_updates(self):
        workspace = Workspace.objects.create(name="Concurrent Co")
        account = AICreditAccount.objects.create(workspace=workspace, token_budget=1_000_000)

        n_threads = 8
        tokens_per_call = 100
        errors = []

        def spend():
            # SQLite (this test suite's DB) serializes writes at the whole-
            # database level rather than row-level like Postgres (the prod
            # DB), so genuine concurrent writers can hit a transient
            # "database is locked" error that Postgres's select_for_update()
            # row locks wouldn't produce -- retry on that specific error so
            # the test still proves the actual property under test (no lost
            # updates), rather than flaking on a SQLite-only artifact.
            for attempt in range(20):
                try:
                    record_spend(account, tokens_per_call, "playbook")
                    return
                except OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 19:
                        errors.append(exc)
                        return
                    time.sleep(0.05)
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(exc)
                    return
                finally:
                    close_old_connections()

        threads = [threading.Thread(target=spend) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        account.refresh_from_db()
        self.assertEqual(account.tokens_used, n_threads * tokens_per_call)
        self.assertEqual(AICreditTransaction.objects.filter(account=account).count(), n_threads)


class ExhaustedBudgetSyncPathTests(TestCase):
    """rewrite_context_note_with_ai: a sync, in-request call site. An
    exhausted account must fall back to the deterministic rewrite without
    ever reaching the Gemini client."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Sync Path Co")
        self.user = User.objects.create_user(username="bob", password="pw")
        self.session = AssessmentSession.objects.create(
            owner_client_id="c3", user=self.user, workspace=self.workspace,
        )
        AICreditAccount.objects.create(workspace=self.workspace, token_budget=100, tokens_used=100)

    def test_exhausted_account_uses_fallback_without_calling_gemini(self):
        from gtm.ai_services import rewrite_context_note_with_ai

        fake_client = MagicMock()
        with patch("gtm.ai_services._get_client", return_value=fake_client), \
             patch("gtm.ai_services._quota_cooldown_active", return_value=False):
            result = rewrite_context_note_with_ai(
                "we sort of have a process but its inconsistent",
                question_text="Do you have a documented onboarding process?",
                session=self.session,
            )

        self.assertTrue(result)  # deterministic fallback is non-empty
        fake_client.models.generate_content.assert_not_called()


class ExhaustedBudgetAsyncPathTests(TestCase):
    """_kickoff_playbook_generation: an async/background call site. An
    exhausted account must not spawn a generation thread and must mark the
    snapshot with the distinct "no_credits" status (not "failed")."""

    def setUp(self):
        self.workspace = Workspace.objects.create(name="Async Path Co")
        self.user = User.objects.create_user(username="carol", password="pw")
        self.session = AssessmentSession.objects.create(
            owner_client_id="c4", user=self.user, workspace=self.workspace,
        )
        self.snapshot = ResultSnapshot.objects.create(
            session=self.session, overall=50.0, category_breakdown={},
            ai_playbook_status="pending",
        )
        AICreditAccount.objects.create(workspace=self.workspace, token_budget=100, tokens_used=100)

    def test_exhausted_account_blocks_kickoff_and_sets_no_credits_status(self):
        from gtm.services import _kickoff_playbook_generation

        with patch("gtm.services.run_in_background") as mock_run:
            spawned = _kickoff_playbook_generation(self.snapshot, session_id=self.session.uuid)

        self.assertFalse(spawned)
        mock_run.assert_not_called()
        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.ai_playbook_status, "no_credits")
