"""
Smoke tests for the three document-export entry points, added alongside the
PDF/DOCX branding pass (gtm/utils_pdf.py, gtm/utils_docx.py): confirms
generation still succeeds and returns the right content-type after switching
colors/fonts/logo, since nothing previously covered these views at all.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models import AgentDocument, AssessmentSession, ResultSnapshot
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class DocumentExportBrandingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="export-user", password="pass1234", email="export@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Export Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="export-client", user=cls.user, workspace=cls.workspace,
            company_name="Export Co",
        )
        cls.snapshot = ResultSnapshot.objects.create(
            session=cls.session, overall=62.0,
            category_breakdown=[{"name": "Demand", "avg": 55.0}],
            ai_playbook="## Priority 1: Fix onboarding (Delivery)\n- Do the thing\n- Do another thing",
        )
        cls.document = AgentDocument.objects.create(
            workspace=cls.workspace, session=cls.session,
            agent_type="gtm_strategist", doc_type="client_summary",
            title="Client Summary", content="## Priority 1: Fix onboarding (Delivery)\n- Do the thing",
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def test_assessment_report_pdf_generates(self):
        response = self.client.get(reverse("gtm:download", args=[self.session.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_agent_document_pdf_export_generates(self):
        url = reverse("document_export", args=[self.document.id, "pdf"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_agent_document_docx_export_generates(self):
        url = reverse("document_export", args=[self.document.id, "docx"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    def test_insight_pdf_export_generates(self):
        url = reverse("insight_export", args=[self.snapshot.pk, "pdf"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_insight_docx_export_generates(self):
        url = reverse("insight_export", args=[self.snapshot.pk, "docx"])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
