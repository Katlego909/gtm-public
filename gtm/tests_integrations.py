"""
Tests for the workspace integrations foundation: credential encryption
(gtm/integrations/crypto.py) and the WorkspaceIntegration model. View-level
permission and save/disconnect tests live in dashboard/tests_integrations_hub.py.
"""

from django.test import TestCase

from gtm.integrations.crypto import CredentialDecryptionError, decrypt_text, encrypt_text
from gtm.models_integrations import WorkspaceIntegration
from gtm.models_workspace import Workspace


class CryptoRoundTripTests(TestCase):
    def test_encrypt_decrypt_round_trip(self):
        ciphertext = encrypt_text("pat-na1-secret-token")
        self.assertNotEqual(ciphertext, "pat-na1-secret-token")
        self.assertEqual(decrypt_text(ciphertext), "pat-na1-secret-token")

    def test_decrypt_tampered_ciphertext_raises(self):
        ciphertext = encrypt_text("pat-na1-secret-token")
        tampered = ciphertext[:-2] + ("aa" if ciphertext[-2:] != "aa" else "bb")
        with self.assertRaises(CredentialDecryptionError):
            decrypt_text(tampered)

    def test_decrypt_garbage_raises(self):
        with self.assertRaises(CredentialDecryptionError):
            decrypt_text("not-a-real-token")


class WorkspaceIntegrationCredentialTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Credential Co")

    def test_set_and_get_credential_round_trip(self):
        integration = WorkspaceIntegration.objects.create(workspace=self.workspace, provider="hubspot")
        integration.set_credential("pat-na1-abcd1234")
        integration.save()

        integration.refresh_from_db()
        self.assertEqual(integration.get_credential(), "pat-na1-abcd1234")
        self.assertEqual(integration.token_preview, "1234")
        self.assertNotIn("pat-na1-abcd1234", integration.encrypted_credentials)

    def test_get_credential_without_stored_value_raises(self):
        integration = WorkspaceIntegration.objects.create(workspace=self.workspace, provider="hubspot")
        with self.assertRaises(CredentialDecryptionError):
            integration.get_credential()

    def test_clear_credential(self):
        integration = WorkspaceIntegration.objects.create(workspace=self.workspace, provider="hubspot")
        integration.set_credential("pat-na1-abcd1234")
        integration.save()

        integration.clear_credential()
        integration.save()

        integration.refresh_from_db()
        self.assertEqual(integration.encrypted_credentials, "")
        self.assertEqual(integration.token_preview, "")
