"""
Tests for the workspace integrations foundation: credential encryption
(gtm/integrations/crypto.py), the WorkspaceIntegration model, and permission
gating on the workspace integrations views. HubSpot network calls are always
mocked -- these tests never hit the real HubSpot API.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.integrations.crypto import CredentialDecryptionError, decrypt_text, encrypt_text
from gtm.models_integrations import WorkspaceIntegration
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


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


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceIntegrationsViewPermissionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="int-admin", password="pass1234", email="admin@test.com")
        self.manager = User.objects.create_user(username="int-manager", password="pass1234", email="manager@test.com")
        self.contributor = User.objects.create_user(username="int-contrib", password="pass1234", email="contrib@test.com")
        self.consultant = User.objects.create_user(username="int-consultant", password="pass1234", email="consultant@test.com")

        self.workspace = Workspace.objects.create(name="Permission Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.admin, role="admin", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.manager, role="manager", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.contributor, role="contributor", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.consultant, role="funti3r_consultant", is_active=True)

        self.url = reverse("workspace:integrations", args=[self.workspace.id])

    def test_manager_is_denied_stricter_than_invite_permission(self):
        """can_manage_integrations deliberately excludes 'manager', unlike can_invite_users."""
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_contributor_is_denied(self):
        self.client.force_login(self.contributor)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_admin_can_view_page(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_funti3r_consultant_can_view_page(self):
        self.client.force_login(self.consultant)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    @mock.patch("gtm.views_workspace.HubSpotClient")
    def test_admin_can_save_token_and_status_reflects_connection_test(self, mock_client_cls):
        mock_client_cls.return_value.test_connection.return_value = (True, "Connected.")
        self.client.force_login(self.admin)

        response = self.client.post(self.url, {"token": "pat-na1-newtoken"}, follow=True)

        self.assertEqual(response.status_code, 200)
        integration = WorkspaceIntegration.objects.get(workspace=self.workspace, provider="hubspot")
        self.assertEqual(integration.status, "connected")
        self.assertEqual(integration.token_preview, "oken")
        self.assertEqual(integration.configured_by, self.admin)

    @mock.patch("gtm.views_workspace.HubSpotClient")
    def test_save_token_with_failed_connection_test_marks_error(self, mock_client_cls):
        mock_client_cls.return_value.test_connection.return_value = (False, "HubSpot rejected this token.")
        self.client.force_login(self.admin)

        self.client.post(self.url, {"token": "pat-na1-badtoken"}, follow=True)

        integration = WorkspaceIntegration.objects.get(workspace=self.workspace, provider="hubspot")
        self.assertEqual(integration.status, "error")
        self.assertEqual(integration.last_error, "HubSpot rejected this token.")

    def test_disconnect_denied_for_contributor(self):
        WorkspaceIntegration.objects.create(workspace=self.workspace, provider="hubspot", status="connected")
        self.client.force_login(self.contributor)
        url = reverse("workspace:integration_disconnect_hubspot", args=[self.workspace.id])

        response = self.client.post(url)

        self.assertEqual(response.status_code, 403)

    def test_disconnect_clears_credential_but_keeps_row(self):
        integration = WorkspaceIntegration.objects.create(workspace=self.workspace, provider="hubspot")
        integration.set_credential("pat-na1-tobeleft")
        integration.status = "connected"
        integration.save()

        self.client.force_login(self.admin)
        url = reverse("workspace:integration_disconnect_hubspot", args=[self.workspace.id])
        self.client.post(url, follow=True)

        integration.refresh_from_db()
        self.assertEqual(integration.status, "not_connected")
        self.assertEqual(integration.encrypted_credentials, "")
