"""
Tests for the global search endpoint (dashboard/views/search.py): workspace
scoping, minimum-query-length gating, per-category matching, and the
no-workspace personal fallback.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models import ActionItem, AgentDocument, AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership
from dashboard.models import Resource

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class GlobalSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="searcher", password="pass1234", email="searcher@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Search Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )
        cls.other_workspace = Workspace.objects.create(name="Other Co")

        cls.session = AssessmentSession.objects.create(
            owner_client_id="search-client",
            user=cls.user,
            workspace=cls.workspace,
            company_name="Ardent SA (Pty) Ltd",
        )
        cls.other_session = AssessmentSession.objects.create(
            owner_client_id="other-client",
            workspace=cls.other_workspace,
            company_name="Ardent Rivals Inc",
        )

        cls.task = ActionItem.objects.create(
            workspace=cls.workspace, note="Define ICP inclusion criteria", status="todo"
        )
        cls.resource = Resource.objects.create(
            workspace=cls.workspace, name="Sales Deck Q3", description="Latest pitch deck"
        )
        cls.document = AgentDocument.objects.create(
            workspace=cls.workspace, agent_type="gtm_strategist", doc_type="roadmap",
            title="Ardent GTM Roadmap", content="Roadmap content",
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def test_query_below_minimum_length_returns_no_results(self):
        response = self.client.get(reverse("global_search"), {"q": "a"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Keep typing")
        self.assertNotContains(response, "Ardent")

    def test_finds_assessment_by_company_name(self):
        response = self.client.get(reverse("global_search"), {"q": "Ardent"})
        self.assertContains(response, "Ardent SA (Pty) Ltd")
        self.assertNotContains(response, "Ardent Rivals Inc")

    def test_finds_task_by_note(self):
        response = self.client.get(reverse("global_search"), {"q": "ICP"})
        self.assertContains(response, "Define ICP inclusion criteria")

    def test_finds_resource_by_name_or_description(self):
        response = self.client.get(reverse("global_search"), {"q": "pitch deck"})
        self.assertContains(response, "Sales Deck Q3")

    def test_finds_document_by_title(self):
        response = self.client.get(reverse("global_search"), {"q": "Roadmap"})
        self.assertContains(response, "Ardent GTM Roadmap")

    def test_no_matches_shows_empty_state(self):
        response = self.client.get(reverse("global_search"), {"q": "nonexistentxyz"})
        self.assertContains(response, "No results")

    def test_other_workspace_data_is_not_leaked(self):
        response = self.client.get(reverse("global_search"), {"q": "Rivals"})
        self.assertNotContains(response, "Ardent Rivals Inc")

    def test_no_workspace_falls_back_to_personal_scope(self):
        personal_user = User.objects.create_user(
            username="personal", password="pass1234", email="personal@test.com"
        )
        AssessmentSession.objects.create(
            owner_client_id="personal-client",
            user=personal_user,
            company_name="Personal Co",
        )
        self.client.force_login(personal_user)
        session = self.client.session
        session.pop("current_workspace_id", None)
        session.save()

        response = self.client.get(reverse("global_search"), {"q": "Personal"})
        self.assertContains(response, "Personal Co")

    def test_no_workspace_scope_excludes_resources_and_documents(self):
        """Resources/Documents are workspace-only concepts -- confirm the
        personal-mode fallback doesn't crash or leak workspace-scoped data."""
        personal_user = User.objects.create_user(
            username="personal2", password="pass1234", email="personal2@test.com"
        )
        self.client.force_login(personal_user)
        session = self.client.session
        session.pop("current_workspace_id", None)
        session.save()

        response = self.client.get(reverse("global_search"), {"q": "Sales Deck"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Sales Deck Q3")
