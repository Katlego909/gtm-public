"""
Characterization smoke tests for the split view packages.

The former monolithic gtm/views.py and dashboard/views.py were split into
packages of domain modules. These tests render the main argument-less GET
pages from every new module as a logged-in workspace member, guarding the
split (and future refactors) against wiring mistakes: a missing re-export,
a broken helper import, or an unresolvable URL fails loudly here.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

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
        "refresh_recent_agent_actions",  # views/hub.py
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
