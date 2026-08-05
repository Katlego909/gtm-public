"""
Beta launch safety net: gtm/ai_credits.py's global daily AI spend ceiling
and gtm/models_workspace.py::Workspace.user_at_creation_cap.
"""
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm import utils_locks
from gtm.ai_credits import can_spend, record_spend, resolve_account
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


class GlobalDailyCapTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Cap Co")
        self.account = resolve_account(workspace=self.workspace)

    def test_uncapped_by_default(self):
        record_spend(self.account, 10_000_000, "playbook")
        result = can_spend(account=self.account)
        self.assertTrue(result.allowed)

    @override_settings(AI_GLOBAL_DAILY_TOKEN_CAP=1000)
    def test_global_cap_blocks_once_aggregate_total_reached(self):
        record_spend(self.account, 999, "playbook")
        self.assertTrue(can_spend(account=self.account).allowed)

        record_spend(self.account, 1, "playbook")
        result = can_spend(account=self.account)
        self.assertFalse(result.allowed)
        self.assertEqual(result.reason, "global_cap")

    @override_settings(AI_GLOBAL_DAILY_TOKEN_CAP=1000)
    def test_global_cap_is_aggregate_across_accounts(self):
        other_workspace = Workspace.objects.create(name="Other Cap Co")
        other_account = resolve_account(workspace=other_workspace)
        record_spend(self.account, 600, "playbook")
        record_spend(other_account, 500, "playbook")

        # Neither account is individually near its own 500k budget, but the
        # combined total (1100) exceeds the global cap -- this is the whole
        # point of the aggregate check.
        self.assertFalse(can_spend(account=self.account).allowed)
        self.assertFalse(can_spend(account=other_account).allowed)

    @override_settings(AI_GLOBAL_DAILY_TOKEN_CAP=1000, BETA_ALERT_EMAIL="ops@funti3r.xyz")
    def test_alert_email_sent_once_not_on_every_blocked_request(self):
        record_spend(self.account, 1000, "playbook")

        can_spend(account=self.account)
        can_spend(account=self.account)
        can_spend(account=self.account)

        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Global AI daily spend cap", mail.outbox[0].subject)

    def tearDown(self):
        utils_locks.release("gtm:ai:global_cap_alert_sent")


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceCreationCapTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="capuser", password="pass1234", email="cap@test.com")
        self.staff_user = User.objects.create_user(
            username="staffuser", password="pass1234", email="staff@test.com", is_staff=True,
        )
        self.client.force_login(self.user)

    def test_first_workspace_creation_succeeds(self):
        response = self.client.post(reverse("gtm:workspace:create"), {"name": "First Co"})
        self.assertTrue(Workspace.objects.filter(name="First Co").exists())
        self.assertEqual(response.status_code, 302)

    def test_second_workspace_creation_blocked_at_default_cap(self):
        self.client.post(reverse("gtm:workspace:create"), {"name": "First Co"})
        response = self.client.post(reverse("gtm:workspace:create"), {"name": "Second Co"})
        self.assertFalse(Workspace.objects.filter(name="Second Co").exists())
        self.assertEqual(response.status_code, 200)  # re-renders the form with an error, no redirect

    @override_settings(MAX_WORKSPACES_PER_USER=2)
    def test_cap_is_configurable(self):
        self.client.post(reverse("gtm:workspace:create"), {"name": "First Co"})
        self.client.post(reverse("gtm:workspace:create"), {"name": "Second Co"})
        response = self.client.post(reverse("gtm:workspace:create"), {"name": "Third Co"})
        self.assertTrue(Workspace.objects.filter(name="Second Co").exists())
        self.assertFalse(Workspace.objects.filter(name="Third Co").exists())

    def test_staff_are_exempt_from_cap(self):
        self.client.force_login(self.staff_user)
        self.client.post(reverse("gtm:workspace:create"), {"name": "Staff Co 1"})
        self.client.post(reverse("gtm:workspace:create"), {"name": "Staff Co 2"})
        self.assertTrue(Workspace.objects.filter(name="Staff Co 1").exists())
        self.assertTrue(Workspace.objects.filter(name="Staff Co 2").exists())

    def test_dashboard_create_endpoint_respects_same_cap(self):
        self.client.post(reverse("gtm:workspace:create"), {"name": "First Co"})
        response = self.client.post(reverse("create_workspace_dashboard"), {"name": "Second Via Dashboard"})
        self.assertFalse(Workspace.objects.filter(name="Second Via Dashboard").exists())
        self.assertContains(response, "workspace limit")
