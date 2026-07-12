"""
Characterization smoke tests for the split view packages.

The former monolithic gtm/views.py and dashboard/views.py were split into
packages of domain modules. These tests render the main argument-less GET
pages from every new module as a logged-in workspace member, guarding the
split (and future refactors) against wiring mistakes: a missing re-export,
a broken helper import, or an unresolvable URL fails loudly here.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models import ActionItem, AgentDocument
from gtm.models import AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class ViewPackageSmokeTests(TestCase):
    """Every argument-less GET page across both view packages must render."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="smoke", password="pass1234", email="smoke@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Smoke Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    # (url name, kwargs) — argument-less GET endpoints only; pages needing an
    # existing object (pk/uuid args) are exercised by their own feature tests.
    GTM_PAGES = [
        "gtm:landing",       # views/assessment.py
        "gtm:history",       # views/profile.py
        "gtm:profile",       # views/profile.py
    ]
    DASHBOARD_PAGES = [
        "dashboard",                  # views/hub.py
        "tasks_board",                # views/hub.py
        "agent_hub",                  # views/hub.py
        "workspace_hub",              # views/hub.py
        "notifications_panel",        # views/pages.py
        "profile",                    # views/pages.py
        "settings",                   # views/pages.py
        "analytics",                  # views/analytics.py
        "kpi_total_sessions",         # views/analytics.py
        "kpi_completed_items",        # views/analytics.py
        "kpi_pending_items",          # views/analytics.py
        "gap_analysis_table",         # views/gap_analysis.py
        "gap_report",                 # views/gap_analysis.py
        "refresh_gap_analysis_table", # views/gap_analysis.py
        "refresh_gap_suggestions",    # views/gap_analysis.py
        "refresh_action_items",       # views/actions.py
        "refresh_resources",          # views/resources.py
        "asset_library",              # views/resources.py
    ]

    def _assert_renders(self, url_name):
        response = self.client.get(reverse(url_name))
        self.assertIn(
            response.status_code,
            (200, 302),
            msg=f"{url_name} returned {response.status_code}",
        )

    def test_gtm_pages_render(self):
        for name in self.GTM_PAGES:
            with self.subTest(url=name):
                self._assert_renders(name)

    def test_dashboard_pages_render(self):
        for name in self.DASHBOARD_PAGES:
            with self.subTest(url=name):
                self._assert_renders(name)

    def test_agent_hub_team_tab_renders(self):
        """?agent_type=team is the 5th agent_hub tab (gtm/team_chat.py) --
        must render both unbound (workspace only) and with an assessment
        bound, since binding one changes which transfer tools exist."""
        response = self.client.get(reverse("agent_hub"), {"agent_type": "team"})
        self.assertEqual(response.status_code, 200)

        session = AssessmentSession.objects.create(
            owner_client_id="smoke-client",
            user=self.user,
            workspace=self.workspace,
            company_name="Smoke Co",
        )
        response = self.client.get(
            reverse("agent_hub"), {"agent_type": "team", "agent_session": str(session.uuid)}
        )
        self.assertEqual(response.status_code, 200)

    def test_team_agent_chat_url_resolves(self):
        """The Team Chat POST endpoint (gtm/team_chat.py's entry point) must
        be wired up; the POST itself needs a live Gemini call so isn't
        exercised here, matching the other 4 chat endpoints."""
        self.assertTrue(reverse("dashboard_team_agent_api"))


@override_settings(SECURE_SSL_REDIRECT=False)
class ActionItemAICompletionTests(TestCase):
    """gtm/action_item_completion.py's on-demand "Complete with AI" flow
    (dashboard/views/actions.py). The actual Gemini turn needs a live call
    (matching every other AI endpoint in this repo, none of which are
    exercised end-to-end here) -- these tests cover the deterministic glue
    around it instead: eligibility gates, permissions, locking, and the
    background-job/polling wiring, with complete_action_item mocked as a
    black box."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="ai-complete", password="pass1234", email="ai-complete@test.com"
        )
        cls.workspace = Workspace.objects.create(name="AI Complete Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="ai-complete-client",
            user=cls.user,
            workspace=cls.workspace,
            company_name="AI Complete Co",
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def _make_item(self, **kwargs):
        defaults = dict(
            session=self.session,
            workspace=self.workspace,
            note="Define ICP inclusion/exclusion criteria and publish one-page version.",
            status="todo",
        )
        defaults.update(kwargs)
        return ActionItem.objects.create(**defaults)

    def test_no_linked_session_is_ineligible(self):
        item = self._make_item(session=None)
        response = self.client.post(reverse("complete_action_item_ai", args=[item.id]))
        self.assertEqual(response.status_code, 400)
        self.assertIn("assessment", response.json()["error"])

    def test_non_todo_item_is_rejected(self):
        item = self._make_item(status="doing")
        response = self.client.post(reverse("complete_action_item_ai", args=[item.id]))
        self.assertEqual(response.status_code, 400)

    @mock.patch("dashboard.views.actions.run_in_background", side_effect=lambda target, *args, **kwargs: target(*args))
    @mock.patch("gtm.action_item_completion.complete_action_item")
    def test_complete_action_item_ai_runs_and_polls_to_done(self, mock_complete, mock_run_bg):
        """run_in_background is patched to run synchronously so the status
        endpoint can be polled deterministically right after the POST."""
        mock_complete.return_value = {
            "success": True,
            "status": "done",
            "summary": "Drafted the ICP one-pager grounded in this company's assessment data.",
            "document_id": "11111111-1111-1111-1111-111111111111",
        }
        item = self._make_item()

        response = self.client.post(reverse("complete_action_item_ai", args=[item.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        mock_complete.assert_called_once()

        status_response = self.client.get(reverse("complete_action_item_ai_status", args=[item.id]))
        self.assertEqual(status_response.status_code, 200)
        status_data = status_response.json()
        self.assertEqual(status_data["state"], "done")
        self.assertEqual(status_data["status"], "done")

    def test_lock_prevents_concurrent_double_trigger(self):
        """Simulates an already-in-flight job by pre-acquiring the same
        lock key the view uses, then confirms a second POST doesn't start
        a duplicate run."""
        from gtm.ai_services import _acquire_lock
        from dashboard.views.actions import _action_item_complete_lock_key

        item = self._make_item()
        self.assertTrue(_acquire_lock(_action_item_complete_lock_key(item.id), ttl_seconds=60))

        with mock.patch("gtm.action_item_completion.complete_action_item") as mock_complete:
            response = self.client.post(reverse("complete_action_item_ai", args=[item.id]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "running")
            mock_complete.assert_not_called()

    def test_non_member_cannot_trigger_completion(self):
        outsider = User.objects.create_user(username="outsider", password="pass1234", email="outsider@test.com")
        self.client.force_login(outsider)
        item = self._make_item()
        url = reverse("complete_action_item_ai", args=[item.id]) + f"?workspace={self.workspace.id}"
        response = self.client.post(url)
        self.assertEqual(response.status_code, 403)


@override_settings(SECURE_SSL_REDIRECT=False)
class UnifiedLibraryTests(TestCase):
    """asset_library's two tabs (dashboard/views/resources.py): ?tab=resources
    absorbs the former standalone Resource Library page, ?tab=documents is
    new -- AgentDocuments grouped by doc_type. Also covers document_list_api
    (dashboard/views/workspace_agent_api.py) excluding action-item
    deliverables from the chat sidebar while keeping other doc types."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="library", password="pass1234", email="library@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Library Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def test_resources_tab_renders_by_default(self):
        response = self.client.get(reverse("asset_library"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "resources")

    def test_documents_tab_lists_seeded_document(self):
        AgentDocument.objects.create(
            workspace=self.workspace,
            agent_type="gtm_strategist",
            doc_type="action_item_deliverable",
            title="Seeded ICP One-Pager",
            content="# ICP",
            created_by=self.user,
        )
        response = self.client.get(reverse("asset_library"), {"tab": "documents"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_tab"], "documents")
        self.assertContains(response, "Seeded ICP One-Pager")

    def test_document_list_api_excludes_action_item_deliverables(self):
        AgentDocument.objects.create(
            workspace=self.workspace,
            agent_type="gtm_strategist",
            doc_type="action_item_deliverable",
            title="Should Be Hidden From Sidebar",
            content="# hidden",
            created_by=self.user,
        )
        AgentDocument.objects.create(
            workspace=self.workspace,
            agent_type="portfolio",
            doc_type="client_summary",
            title="Should Still Show In Sidebar",
            content="# visible",
            created_by=self.user,
        )
        response = self.client.get(
            reverse("document_list_api"),
            {"workspace": str(self.workspace.id), "exclude_doc_type": "action_item_deliverable"},
        )
        self.assertEqual(response.status_code, 200)
        titles = [d["title"] for d in response.json()["documents"]]
        self.assertNotIn("Should Be Hidden From Sidebar", titles)
        self.assertIn("Should Still Show In Sidebar", titles)
