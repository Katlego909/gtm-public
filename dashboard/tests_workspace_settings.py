"""
Tests for editing and deleting a workspace from the dashboard Workspace Hub
(dashboard/views/pages.py::edit_workspace, delete_workspace). Delete is a
soft-delete (Workspace.is_active=False) -- see gtm/models_workspace.py and
the WorkspaceMiddleware / dashboard workspace-resolution queries that must
respect it. Mirrors the setup/style of dashboard/tests_integrations_hub.py.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceEditTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="edit-admin", password="pass1234", email="edit-admin@test.com")
        self.consultant = User.objects.create_user(username="edit-consultant", password="pass1234", email="edit-consultant@test.com")
        self.manager = User.objects.create_user(username="edit-manager", password="pass1234", email="edit-manager@test.com")
        self.contributor = User.objects.create_user(username="edit-contrib", password="pass1234", email="edit-contrib@test.com")
        self.viewer = User.objects.create_user(username="edit-viewer", password="pass1234", email="edit-viewer@test.com")
        self.workspace = Workspace.objects.create(name="Edit Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.admin, role="admin", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.consultant, role="funti3r_consultant", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.manager, role="manager", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.contributor, role="contributor", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.viewer, role="viewer", is_active=True)
        self.url = reverse("edit_workspace", args=[self.workspace.id])

    def test_admin_can_get_edit_form(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Workspace Settings")

    def test_admin_can_update_workspace(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, {
            "name": "Edit Co Renamed",
            "company_size": "11-50",
            "industry": "SaaS/Software",
            "website": "https://example.com",
        })
        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Edit Co Renamed")
        self.assertEqual(self.workspace.industry, "SaaS/Software")
        self.assertIn("workspaceUpdated", response.headers.get("HX-Trigger", ""))

    def test_consultant_can_update_workspace(self):
        self.client.force_login(self.consultant)
        response = self.client.post(self.url, {
            "name": "Consultant Edited", "company_size": "", "industry": "", "website": "",
        })
        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Consultant Edited")

    def test_manager_cannot_edit(self):
        self.client.force_login(self.manager)
        response = self.client.post(self.url, {"name": "Sneaky Rename"})
        self.assertEqual(response.status_code, 403)
        self.workspace.refresh_from_db()
        self.assertEqual(self.workspace.name, "Edit Co")

    def test_contributor_cannot_edit(self):
        self.client.force_login(self.contributor)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_viewer_cannot_edit(self):
        self.client.force_login(self.viewer)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceDeleteTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="del-admin", password="pass1234", email="del-admin@test.com")
        self.consultant = User.objects.create_user(username="del-consultant", password="pass1234", email="del-consultant@test.com")
        self.manager = User.objects.create_user(username="del-manager", password="pass1234", email="del-manager@test.com")
        self.workspace = Workspace.objects.create(name="Delete Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.admin, role="admin", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.consultant, role="funti3r_consultant", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.manager, role="manager", is_active=True)
        self.url = reverse("delete_workspace", args=[self.workspace.id])

    def test_admin_can_get_confirm_form(self):
        self.client.force_login(self.admin)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete Workspace")

    def test_admin_delete_with_wrong_name_does_not_delete(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, {"confirm_name": "not the right name"})
        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertTrue(self.workspace.is_active)
        self.assertContains(response, "does not match")

    def test_admin_delete_with_correct_name_deactivates(self):
        self.client.force_login(self.admin)
        response = self.client.post(self.url, {"confirm_name": "Delete Co"})
        self.assertEqual(response.status_code, 200)
        self.workspace.refresh_from_db()
        self.assertFalse(self.workspace.is_active)
        self.assertContains(response, "Workspace Deleted")

    def test_consultant_cannot_delete(self):
        self.client.force_login(self.consultant)
        response = self.client.post(self.url, {"confirm_name": "Delete Co"})
        self.assertEqual(response.status_code, 403)
        self.workspace.refresh_from_db()
        self.assertTrue(self.workspace.is_active)

    def test_manager_cannot_delete(self):
        self.client.force_login(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403)


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceVisibilityAfterDeleteTests(TestCase):
    """Confirms the is_active visibility fix -- a soft-deleted workspace must
    disappear from the dashboard's workspace selector for its members."""

    def setUp(self):
        self.admin = User.objects.create_user(username="vis-admin", password="pass1234", email="vis-admin@test.com")
        self.workspace_a = Workspace.objects.create(name="Keep Co")
        self.workspace_b = Workspace.objects.create(name="Gone Co")
        WorkspaceMembership.objects.create(workspace=self.workspace_a, user=self.admin, role="admin", is_active=True)
        WorkspaceMembership.objects.create(workspace=self.workspace_b, user=self.admin, role="admin", is_active=True)

    def test_deleted_workspace_disappears_from_hub_selector(self):
        self.client.force_login(self.admin)
        delete_url = reverse("delete_workspace", args=[self.workspace_b.id])
        response = self.client.post(delete_url, {"confirm_name": "Gone Co"})
        self.assertEqual(response.status_code, 200)

        response = self.client.get(reverse("workspace_hub"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Keep Co")
        self.assertNotContains(response, "Gone Co")
