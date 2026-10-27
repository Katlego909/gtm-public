"""
Tests for gtm/ai_auditor.py's two save paths: GTMFile (session evidence) and
dashboard.Resource (Library asset). The Gemini call and credit gate are mocked;
these tests only check that each path reads the file and writes its own fields.
"""

import shutil
import tempfile
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from dashboard.models import Resource
from gtm import ai_auditor
from gtm.models import GTMFile
from gtm.models_workspace import Workspace

AUDIT_TEXT = "## Verdict\nStrong positioning.\nSCORE_MODIFIER: +1\nGTM_CATEGORIES: Demand"


class AuditSavePathTests(TestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self.media_root)
        override.enable()
        self.addCleanup(override.disable)
        self.workspace = Workspace.objects.create(name="Audit Co")
        patch_budget = mock.patch.object(ai_auditor, "_request_budget_available", return_value=True)
        patch_budget.start()
        self.addCleanup(patch_budget.stop)

    def _upload(self):
        return SimpleUploadedFile("deck.pdf", b"%PDF-1.4 test", content_type="application/pdf")

    @mock.patch.object(ai_auditor, "_run_multimodal_audit", return_value=AUDIT_TEXT)
    def test_gtm_file_audit_saves_notes(self, mock_audit):
        gtm_file = GTMFile.objects.create(workspace=self.workspace, file=self._upload())

        result = ai_auditor.perform_gtm_visual_audit(gtm_file, session_context="ctx")

        self.assertEqual(result, AUDIT_TEXT)
        self.assertEqual(mock_audit.call_args.args[:2], (b"%PDF-1.4 test", "application/pdf"))
        gtm_file.refresh_from_db()
        self.assertEqual(gtm_file.audit_status, "complete")
        self.assertEqual(gtm_file.ai_audit_notes, AUDIT_TEXT)

    @mock.patch.object(ai_auditor, "_run_multimodal_audit", return_value=AUDIT_TEXT)
    def test_resource_audit_saves_full_text_summary(self, mock_audit):
        resource = Resource.objects.create(
            workspace=self.workspace, name="Deck", resource_type="file", file=self._upload(),
        )

        self.assertTrue(ai_auditor.perform_resource_audit(resource))

        self.assertEqual(mock_audit.call_args.args[:2], (b"%PDF-1.4 test", "application/pdf"))
        resource.refresh_from_db()
        self.assertEqual(resource.audit_status, "complete")
        self.assertEqual(resource.ai_audit_summary, AUDIT_TEXT)
        self.assertIsNotNone(resource.ai_audit_at)

    @mock.patch.object(ai_auditor, "_run_multimodal_audit", return_value="")
    def test_empty_audit_marks_both_failed(self, _mock_audit):
        gtm_file = GTMFile.objects.create(workspace=self.workspace, file=self._upload())
        resource = Resource.objects.create(
            workspace=self.workspace, name="Deck", resource_type="file", file=self._upload(),
        )

        ai_auditor.perform_gtm_visual_audit(gtm_file)
        self.assertFalse(ai_auditor.perform_resource_audit(resource))

        gtm_file.refresh_from_db()
        resource.refresh_from_db()
        self.assertEqual(gtm_file.audit_status, "failed")
        self.assertEqual(resource.audit_status, "failed")
