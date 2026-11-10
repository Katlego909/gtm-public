"""
Guards for the per-request overhead trimmed in the loading-performance pass.

Every authenticated request (including each HTMX polling tick) runs
WorkspaceMiddleware and the workspace_permissions context processor. They used
to resolve the same workspace + membership three or four times and re-write
the signed session cookie on every response. These tests pin the cheaper
behaviour so it can't quietly regress.
"""
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext, override_settings
from django.urls import reverse

from gtm.models_workspace import Workspace, WorkspaceMembership
from gtm.utils_markdown import render_markdown

User = get_user_model()

MEMBERSHIP_TABLE = WorkspaceMembership._meta.db_table
WORKSPACE_TABLE = Workspace._meta.db_table


def _queries_touching(ctx, table):
    return [q["sql"] for q in ctx.captured_queries if f'"{table}"' in q["sql"]]


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceRequestOverheadTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="overhead-user", password="pass1234", email="overhead@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Overhead Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def test_workspace_resolved_with_one_membership_query_per_request(self):
        # A small partial that renders a template (so the context processor
        # runs too): the middleware's joined lookup should be the only
        # workspace/membership query.
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse("notification_unread_count"))
        self.assertEqual(response.status_code, 200)

        membership_queries = _queries_touching(ctx, MEMBERSHIP_TABLE)
        self.assertEqual(len(membership_queries), 1, membership_queries)
        standalone_workspace_queries = [
            sql for sql in _queries_touching(ctx, WORKSPACE_TABLE)
            if f'"{MEMBERSHIP_TABLE}"' not in sql
        ]
        self.assertEqual(standalone_workspace_queries, [])

    def test_unchanged_workspace_does_not_rewrite_session_cookie(self):
        # Signed-cookie sessions re-sign and re-send the cookie on every
        # write; an unchanged workspace pointer must not trigger one.
        # (With signed cookies the test client doesn't send data saved via
        # client.session, so the first request legitimately sets the pointer;
        # the client then carries that cookie, as a browser would.)
        self.client.get(reverse("notification_unread_count"))
        response = self.client.get(reverse("notification_unread_count"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(settings.SESSION_COOKIE_NAME, response.cookies)

    def test_context_processor_still_exposes_workspace_permissions(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_workspace"], self.workspace)
        self.assertTrue(response.context["user_is_workspace_member"])
        self.assertTrue(response.context["user_is_admin"])

    def test_dashboard_does_not_build_agent_chat_context(self):
        # Suggested prompts / chat history are only rendered by the agent hub,
        # which builds its own; the dashboard used to compute them anyway.
        with mock.patch("gtm.ai_chat.build_session_context") as build_ctx:
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        build_ctx.assert_not_called()
        self.assertNotIn("dashboard_agent_prompts", response.context)


class RenderMarkdownReuseTests(TestCase):
    def test_reused_converter_does_not_leak_state_between_documents(self):
        first = str(render_markdown("Text[^1]\n\n[^1]: first note", extensions=["extra"]))
        second = str(render_markdown("Plain paragraph.", extensions=["extra"]))
        self.assertIn("first note", first)
        self.assertNotIn("first note", second)
        self.assertNotIn("footnote", second)

    def test_heading_ids_restart_per_document(self):
        a = str(render_markdown("## Plan", extensions=["toc"]))
        b = str(render_markdown("## Plan", extensions=["toc"]))
        self.assertEqual(a, b)
        self.assertIn('id="plan"', b)

    def test_empty_input_renders_empty(self):
        self.assertEqual(str(render_markdown("")), "")
        self.assertEqual(str(render_markdown(None)), "")
