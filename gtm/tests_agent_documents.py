"""
Tests for gtm/agent_documents.py's read/search additions (get_agent_document,
search_agent_documents) and the read_document/search_documents/search_evidence
tool closures built on top of them in gtm/workspace_agent_chat.py (workspace
agents), gtm/ai_chat.py (Charlie), and gtm/action_item_completion.py (the
one-shot task-completion agent). These are plain Python functions dispatched
by Gemini's automatic function calling, so they're tested directly here
without a live Gemini call, matching the existing convention (see
gtm/tests_agent_actions.py).
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from gtm.action_item_completion import _build_completion_tools
from gtm.agent_documents import get_agent_document, search_agent_documents
from gtm.ai_chat import _build_document_tools_for_session
from gtm.models import ActionItem, AgentDocument, AssessmentSession, CategoryDocument, DeliveryDocument
from gtm.models_workspace import Workspace, WorkspaceMembership
from gtm.workspace_agent_chat import _build_document_tools

User = get_user_model()


class AgentDocumentServiceTests(TestCase):
    """Direct tests of the service-layer functions in agent_documents.py."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="doc-owner", password="pass1234")
        cls.workspace = Workspace.objects.create(name="Doc Co")
        cls.other_workspace = Workspace.objects.create(name="Other Co")
        cls.session = AssessmentSession.objects.create(
            owner_client_id="doc-client", user=cls.user, workspace=cls.workspace, company_name="Doc Co",
        )
        cls.doc = AgentDocument.objects.create(
            workspace=cls.workspace, session=cls.session, agent_type="insights",
            doc_type="roadmap", title="Q3 Roadmap", content="Focus on ICP clarity and SLA definition.",
        )

    def test_get_agent_document_found_in_workspace_scope(self):
        found = get_agent_document(self.doc.pk, workspace=self.workspace)
        self.assertEqual(found, self.doc)

    def test_get_agent_document_not_found_in_wrong_workspace(self):
        found = get_agent_document(self.doc.pk, workspace=self.other_workspace)
        self.assertIsNone(found)

    def test_get_agent_document_found_in_session_scope(self):
        found = get_agent_document(self.doc.pk, session=self.session)
        self.assertEqual(found, self.doc)

    def test_search_agent_documents_matches_title(self):
        results = list(search_agent_documents("Roadmap", workspace=self.workspace))
        self.assertIn(self.doc, results)

    def test_search_agent_documents_matches_content(self):
        results = list(search_agent_documents("SLA definition", workspace=self.workspace))
        self.assertIn(self.doc, results)

    def test_search_agent_documents_no_match(self):
        results = list(search_agent_documents("nonexistent xyz query", workspace=self.workspace))
        self.assertEqual(results, [])


class WorkspaceDocumentToolsTests(TestCase):
    """read_document/search_documents/search_evidence as built by
    _build_document_tools (workspace-scoped: Nora/Theo/Milo)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="ws-doc-user", password="pass1234")
        cls.workspace = Workspace.objects.create(name="Workspace Doc Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True,
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="ws-doc-client", user=cls.user, workspace=cls.workspace, company_name="Workspace Doc Co",
        )
        cls.doc = AgentDocument.objects.create(
            workspace=cls.workspace, session=cls.session, agent_type="insights",
            doc_type="roadmap", title="Q3 Roadmap", content="Focus on ICP clarity.",
        )
        cls.delivery_doc = DeliveryDocument.objects.create(
            session=cls.session, original_filename="contract.pdf",
            extracted_text="This SLA guarantees a 4 hour response time for all critical tickets.",
        )
        cls.category_doc = CategoryDocument.objects.create(
            session=cls.session, category="demand", original_filename="icp.csv",
            extracted_text="Our ideal customer profile targets mid-market SaaS companies.",
        )

    def _tools(self):
        return {t.__name__: t for t in _build_document_tools("insights", self.workspace, user=self.user)}

    def test_read_document_returns_content(self):
        result = self._tools()["read_document"](str(self.doc.pk))
        self.assertIn("Q3 Roadmap", result)
        self.assertIn("Focus on ICP clarity.", result)

    def test_read_document_not_found(self):
        result = self._tools()["read_document"]("00000000-0000-0000-0000-000000000000")
        self.assertIn("couldn't find", result)

    def test_search_documents_finds_match(self):
        result = self._tools()["search_documents"]("ICP clarity")
        self.assertIn(str(self.doc.pk), result)
        self.assertIn("Q3 Roadmap", result)

    def test_search_documents_no_match(self):
        result = self._tools()["search_documents"]("totally nonexistent xyz")
        self.assertIn("No documents matching", result)

    def test_search_evidence_finds_delivery_match(self):
        result = self._tools()["search_evidence"]("4 hour response time")
        self.assertIn("contract.pdf", result)
        self.assertIn("Delivery", result)

    def test_search_evidence_finds_category_match(self):
        result = self._tools()["search_evidence"]("mid-market SaaS")
        self.assertIn("icp.csv", result)
        self.assertIn("Demand", result)

    def test_search_evidence_no_match(self):
        result = self._tools()["search_evidence"]("totally nonexistent xyz")
        self.assertIn("No uploaded evidence matching", result)


class SessionDocumentToolsTests(TestCase):
    """read_document/search_documents/search_evidence as built by
    _build_document_tools_for_session (Charlie)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="session-doc-user", password="pass1234")
        cls.session = AssessmentSession.objects.create(
            owner_client_id="session-doc-client", user=cls.user, company_name="Session Doc Co",
        )
        cls.doc = AgentDocument.objects.create(
            session=cls.session, agent_type="gtm_strategist",
            doc_type="action_plan", title="90 Day Plan", content="Nail down qualification criteria.",
        )
        cls.delivery_doc = DeliveryDocument.objects.create(
            session=cls.session, original_filename="onboarding.docx",
            extracted_text="Time to value target is 14 days from contract signature.",
        )

    def _tools(self):
        return {t.__name__: t for t in _build_document_tools_for_session(self.session, user=self.user)}

    def test_read_document_returns_content(self):
        result = self._tools()["read_document"](str(self.doc.pk))
        self.assertIn("90 Day Plan", result)

    def test_read_document_not_found(self):
        result = self._tools()["read_document"]("00000000-0000-0000-0000-000000000000")
        self.assertIn("couldn't find", result)

    def test_search_documents_finds_match(self):
        result = self._tools()["search_documents"]("qualification criteria")
        self.assertIn(str(self.doc.pk), result)

    def test_search_evidence_finds_match(self):
        result = self._tools()["search_evidence"]("14 days")
        self.assertIn("onboarding.docx", result)


class CompletionAgentDocumentToolsTests(TestCase):
    """read_document/search_documents/search_evidence as built by
    _build_completion_tools (the one-shot task-completion agent)."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="completion-doc-user", password="pass1234")
        cls.session = AssessmentSession.objects.create(
            owner_client_id="completion-doc-client", user=cls.user, company_name="Completion Doc Co",
        )
        cls.action_item = ActionItem.objects.create(
            session=cls.session, note="Draft an SLA policy", status="todo",
        )
        cls.doc = AgentDocument.objects.create(
            session=cls.session, agent_type="gtm_strategist",
            doc_type="action_item_deliverable", title="Draft SLA Policy", content="4 hour first response.",
        )
        cls.delivery_doc = DeliveryDocument.objects.create(
            session=cls.session, original_filename="support_logs.csv",
            extracted_text="Average historical response time was 6 hours across all tickets.",
        )

    def _tools(self):
        return {
            t.__name__: t
            for t in _build_completion_tools(self.action_item, self.user, [])
        }

    def test_read_document_returns_content(self):
        result = self._tools()["read_document"](str(self.doc.pk))
        self.assertIn("Draft SLA Policy", result)

    def test_search_documents_finds_match(self):
        result = self._tools()["search_documents"]("4 hour first response")
        self.assertIn(str(self.doc.pk), result)

    def test_search_evidence_finds_match(self):
        result = self._tools()["search_evidence"]("6 hours")
        self.assertIn("support_logs.csv", result)
