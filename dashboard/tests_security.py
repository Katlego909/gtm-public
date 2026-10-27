"""
Security regression tests: markdown sanitization (stored XSS), cross-workspace
access via a client-supplied ?workspace=, CSRF on the feedback endpoints, and
the production SECRET_KEY guard.
"""

import os
import subprocess
import sys
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.models import BetaFeedback, GapAnalysisMetric
from dashboard.views.helpers import _md
from gtm.models import ActionItem, AssessmentSession, ChatMessage
from gtm.models_workspace import Workspace, WorkspaceMembership
from gtm.templatetags.markdown_filters import markdownify

User = get_user_model()

XSS_NOTE = 'Ship it <img src=x onerror=alert(1)> <script>alert(2)</script>'


class MarkdownSanitizationTests(TestCase):
    def test_markdownify_strips_script_and_handlers(self):
        html = markdownify(XSS_NOTE)
        self.assertNotIn("<script", html)
        self.assertNotIn("onerror", html)
        self.assertIn("Ship it", html)

    def test_chat_markdown_strips_script_and_handlers(self):
        html = _md("**Hi**\n\n" + XSS_NOTE + '\n\n[x](javascript:alert(3))')
        self.assertIn("<strong>Hi</strong>", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("onerror", html)
        self.assertNotIn("javascript:", html)

    def test_toc_anchor_ids_survive(self):
        from gtm.utils_markdown import render_markdown
        html = render_markdown("## Next Moves", extensions=["toc"])
        self.assertIn('id="next-moves"', html)


@override_settings(SECURE_SSL_REDIRECT=False)
class CrossWorkspaceAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.victim = User.objects.create_user(username="victim", password="pass1234", email="victim@test.com")
        cls.attacker = User.objects.create_user(username="attacker", password="pass1234", email="attacker@test.com")
        cls.ws_a = Workspace.objects.create(name="Victim Co")
        cls.ws_b = Workspace.objects.create(name="Attacker Co")
        WorkspaceMembership.objects.create(workspace=cls.ws_a, user=cls.victim, role="admin", is_active=True)
        WorkspaceMembership.objects.create(workspace=cls.ws_b, user=cls.attacker, role="admin", is_active=True)
        cls.session_a = AssessmentSession.objects.create(
            owner_client_id="victim-client", user=cls.victim, workspace=cls.ws_a,
            company_name="Victim Co", is_completed=True,
        )
        cls.metric_a = GapAnalysisMetric.objects.create(
            workspace=cls.ws_a, session=cls.session_a, user=cls.victim,
            category="Sales Efficiency", metric="Win Rate", current=30, target=40,
            priority="High", recommendation="SECRET-VICTIM-RECOMMENDATION", source="AI",
        )
        ActionItem.objects.create(session=cls.session_a, workspace=cls.ws_a, note="victim task", status="todo")
        ChatMessage.objects.create(session=cls.session_a, user=cls.victim, message="hi", response="hello")

    def setUp(self):
        self.client.force_login(self.attacker)
        self.qs = f"?workspace={self.ws_a.id}"

    def test_gap_table_ignores_foreign_workspace_param(self):
        for name in ("gap_analysis_table", "refresh_gap_analysis_table"):
            response = self.client.get(reverse(name) + self.qs)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, "SECRET-VICTIM-RECOMMENDATION")

    def test_gap_row_ignores_foreign_workspace_param(self):
        response = self.client.get(reverse("get_gap_metric_row", args=[self.metric_a.pk]) + self.qs)
        self.assertEqual(response.status_code, 404)

    def test_gap_views_require_login(self):
        anon = Client()
        response = anon.get(reverse("gap_analysis_table") + self.qs)
        self.assertEqual(response.status_code, 302)

    def test_kpi_ignores_foreign_workspace_param(self):
        response = self.client.get(reverse("kpi_pending_items") + self.qs)
        self.assertContains(response, '<div id="pending-items">0</div>')

    def test_agent_clear_rejects_foreign_session(self):
        response = self.client.post(
            reverse("dashboard_agent_clear_api") + self.qs,
            data={"session_id": str(self.session_a.uuid)},
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(ChatMessage.objects.filter(session=self.session_a).exists())

    def test_agent_chat_rejects_foreign_session(self):
        response = self.client.post(
            reverse("dashboard_agent_api") + self.qs,
            data={"session_id": str(self.session_a.uuid), "message": "summarise"},
        )
        self.assertEqual(response.status_code, 404)

    def test_member_can_still_clear_own_session(self):
        self.client.force_login(self.victim)
        response = self.client.post(
            reverse("dashboard_agent_clear_api") + self.qs,
            data={"session_id": str(self.session_a.uuid)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ChatMessage.objects.filter(session=self.session_a).exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class FeedbackCsrfTests(TestCase):
    def test_feedback_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(reverse("general_feedback"), {"feedback": "spam"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(BetaFeedback.objects.exists())


class SecretKeyGuardTests(TestCase):
    def test_debug_off_with_default_key_refuses_to_boot(self):
        # Empty (not absent) so load_dotenv can't refill it from a local .env.
        env = {**os.environ, "DJANGO_SECRET_KEY": "", "DEBUG": "False",
               "DJANGO_SETTINGS_MODULE": "gtm_validator.settings"}
        result = subprocess.run(
            [sys.executable, "-c", "import django; django.setup()"],
            cwd=Path(__file__).resolve().parent.parent, env=env,
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DJANGO_SECRET_KEY must be set", result.stderr)
