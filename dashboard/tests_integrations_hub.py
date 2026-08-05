"""
Tests for the HubSpot integration card embedded in the Workspace Hub
(dashboard/views/hub.py::workspace_hub, dashboard/partials/workspace_hub_content.html).
This is the discoverable, dashboard-SPA-integrated surface for workspace
integrations -- see gtm/tests_integrations.py for the lower-level model and
crypto tests. HubSpot network calls are always mocked.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models_integrations import WorkspaceIntegration
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceHubIntegrationCardTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="hub-admin", password="pass1234", email="admin@test.com")
        self.contributor = User.objects.create_user(username="hub-contrib", password="pass1234", email="contrib@test.com")
        self.workspace = Workspace.objects.create(name="Hub Integration Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.admin, role="admin", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.contributor, role="contributor", is_active=True)
        self.url = reverse("workspace_hub")

    def test_admin_sees_integrations_card(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url, {"workspace": str(self.workspace.id)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Integrations")
        self.assertContains(response, "Not Connected")

    def test_contributor_does_not_see_integrations_card(self):
        self.client.force_login(self.contributor)
        response = self.client.get(self.url, {"workspace": str(self.workspace.id)})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "pat-na1-")

    def test_contributor_post_is_denied(self):
        self.client.force_login(self.contributor)
        response = self.client.post(
            f"{self.url}?workspace={self.workspace.id}",
            {"integration_action": "save_hubspot", "token": "pat-na1-sneaky"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(WorkspaceIntegration.objects.filter(workspace=self.workspace).exists())

    @mock.patch("dashboard.views.hub.HubSpotClient")
    def test_admin_save_hubspot_connects(self, mock_client_cls):
        mock_client_cls.return_value.test_connection.return_value = (True, "Connected.")
        self.client.force_login(self.admin)

        response = self.client.post(
            f"{self.url}?workspace={self.workspace.id}",
            {"integration_action": "save_hubspot", "token": "pat-na1-newtoken"},
        )

        self.assertEqual(response.status_code, 200)
        integration = WorkspaceIntegration.objects.get(workspace=self.workspace, provider="hubspot")
        self.assertEqual(integration.status, "connected")
        self.assertEqual(integration.configured_by, self.admin)
        self.assertContains(response, "Connected")

    @mock.patch("dashboard.views.hub.HubSpotClient")
    def test_admin_disconnect_clears_credential(self, mock_client_cls):
        mock_client_cls.return_value.test_connection.return_value = (True, "Connected.")
        integration = WorkspaceIntegration.objects.create(workspace=self.workspace, provider="hubspot")
        integration.set_credential("pat-na1-existing")
        integration.status = "connected"
        integration.save()

        self.client.force_login(self.admin)
        response = self.client.post(
            f"{self.url}?workspace={self.workspace.id}",
            {"integration_action": "disconnect_hubspot"},
        )

        self.assertEqual(response.status_code, 200)
        integration.refresh_from_db()
        self.assertEqual(integration.status, "not_connected")
        self.assertEqual(integration.encrypted_credentials, "")
