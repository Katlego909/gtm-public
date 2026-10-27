"""
Tests for the inbound metrics API (dashboard/views/inbound_api.py) and its
WorkspaceApiKey management in the Workspace Hub.

Design invariants under test: a key reaches only its own workspace; a pushed
value goes through the same closure rule as a manual check-in (measured value
reaching target closes the gap, nothing else does); the API can't create gaps;
only the key's hash is stored.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.models import GapAnalysisMetric, GapMetricMeasurement
from dashboard.views import inbound_api
from gtm.models_integrations import WorkspaceApiKey
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class InboundApiTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(username="api-admin", password="pass1234", email="api-admin@test.com")
        cls.workspace = Workspace.objects.create(name="API Co")
        cls.other_workspace = Workspace.objects.create(name="Other Co")
        WorkspaceMembership.objects.create(workspace=cls.workspace, user=cls.admin, role="admin", is_active=True)

    def setUp(self):
        self.api_key, self.plaintext = WorkspaceApiKey.generate(self.workspace, "CRM sync", self.admin)
        self.list_url = reverse("api_list_gap_metrics")
        self.measure_url = reverse("api_create_gap_measurement")

    def _make_metric(self, workspace=None, **kwargs):
        defaults = dict(
            workspace=workspace or self.workspace,
            category="Sales Efficiency",
            metric="Win Rate",
            current=30,
            target=40,
            priority="High",
            recommendation="Run deal reviews.",
            source="AI",
        )
        defaults.update(kwargs)
        return GapAnalysisMetric.objects.create(**defaults)

    def _post(self, payload, key=None, raw=None):
        body = raw if raw is not None else json.dumps(payload)
        return self.client.post(
            self.measure_url, data=body, content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {key if key is not None else self.plaintext}",
        )


class ApiKeyModelTests(InboundApiTestCase):
    def test_only_hash_is_stored(self):
        self.assertTrue(self.plaintext.startswith("gtmk_"))
        self.assertNotEqual(self.api_key.key_hash, self.plaintext)
        self.assertEqual(self.api_key.key_hash, WorkspaceApiKey.hash_key(self.plaintext))
        self.assertEqual(self.api_key.prefix, self.plaintext[:12])

    def test_authenticate_records_last_used(self):
        self.assertIsNone(self.api_key.last_used_at)
        self.assertEqual(WorkspaceApiKey.authenticate(self.plaintext), self.api_key)
        self.api_key.refresh_from_db()
        self.assertIsNotNone(self.api_key.last_used_at)


class AuthenticationTests(InboundApiTestCase):
    def test_missing_key_is_401(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 401)

    def test_wrong_key_is_401(self):
        response = self.client.get(self.list_url, HTTP_AUTHORIZATION="Bearer gtmk_not-a-real-key")
        self.assertEqual(response.status_code, 401)

    def test_non_bearer_scheme_is_401(self):
        response = self.client.get(self.list_url, HTTP_AUTHORIZATION=f"Basic {self.plaintext}")
        self.assertEqual(response.status_code, 401)

    def test_revoked_key_is_401(self):
        self._make_metric()
        self.api_key.revoked_at = self.api_key.created_at
        self.api_key.save()
        self.assertEqual(self._post({"metric": "Win Rate", "value": 45}).status_code, 401)
        self.assertFalse(GapMetricMeasurement.objects.exists())

    def test_inactive_workspace_is_401(self):
        self.workspace.is_active = False
        self.workspace.save()
        response = self.client.get(self.list_url, HTTP_AUTHORIZATION=f"Bearer {self.plaintext}")
        self.assertEqual(response.status_code, 401)

    def test_session_login_alone_is_not_enough(self):
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.list_url).status_code, 401)


class ListGapMetricsTests(InboundApiTestCase):
    def test_lists_only_own_workspace(self):
        self._make_metric()
        self._make_metric(workspace=self.other_workspace, metric="Average Deal Size", category="Sales Velocity")
        response = self.client.get(self.list_url, HTTP_AUTHORIZATION=f"Bearer {self.plaintext}")
        self.assertEqual(response.status_code, 200)
        metrics = response.json()["gap_metrics"]
        self.assertEqual([m["metric"] for m in metrics], ["Win Rate"])
        self.assertEqual(metrics[0]["status"], "open")


class CreateMeasurementTests(InboundApiTestCase):
    def test_value_below_target_keeps_gap_open(self):
        metric = self._make_metric()
        response = self._post({"metric": "Win Rate", "value": 35, "note": "From HubSpot"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertFalse(body["closed"])
        self.assertEqual(body["status"], "open")
        metric.refresh_from_db()
        self.assertEqual(metric.current, 35)
        self.assertEqual(metric.estimate_method, "measured")
        measurement = GapMetricMeasurement.objects.get(gap_metric=metric)
        self.assertEqual(measurement.source, "api")
        self.assertEqual(measurement.api_key, self.api_key)
        self.assertIsNone(measurement.recorded_by)
        self.assertEqual(measurement.note, "From HubSpot")

    def test_value_reaching_target_closes_gap(self):
        metric = self._make_metric()
        response = self._post({"metric": "Win Rate", "value": 40})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["closed"])
        metric.refresh_from_db()
        self.assertEqual(metric.status, "closed")
        self.assertEqual(metric.closed_reason, "target_reached")

    def test_cac_payback_closes_when_at_or_below_target(self):
        metric = self._make_metric(metric="CAC Payback Period", category="Marketing ROI", current=18, target=12)
        self.assertFalse(self._post({"metric": "CAC Payback Period", "value": 14}).json()["closed"])
        self.assertTrue(self._post({"metric": "CAC Payback Period", "value": 11.5}).json()["closed"])
        metric.refresh_from_db()
        self.assertEqual(metric.status, "closed")

    def test_unflagged_metric_is_404_and_creates_nothing(self):
        response = self._post({"metric": "Win Rate", "value": 50})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(GapAnalysisMetric.objects.exists())

    def test_cannot_write_other_workspace_metric(self):
        other = self._make_metric(workspace=self.other_workspace)
        response = self._post({"metric": "Win Rate", "value": 50})
        self.assertEqual(response.status_code, 404)
        other.refresh_from_db()
        self.assertEqual(other.current, 30)
        self.assertFalse(GapMetricMeasurement.objects.exists())

    def test_invalid_payloads_are_400(self):
        self._make_metric()
        cases = [
            {"raw": "not json"},
            {"raw": "[1, 2]"},
            {"payload": {"metric": "Happiness", "value": 5}},
            {"payload": {"metric": "Win Rate"}},
            {"payload": {"metric": "Win Rate", "value": "45"}},
            {"payload": {"metric": "Win Rate", "value": True}},
            {"payload": {"metric": "Win Rate", "value": -1}},
            {"raw": '{"metric": "Win Rate", "value": NaN}'},
            {"raw": '{"metric": "Win Rate", "value": Infinity}'},
            {"payload": {"metric": "Win Rate", "value": 45, "note": "x" * 241}},
            {"payload": {"metric": "Win Rate", "value": 45, "note": 7}},
            {"payload": {"metric": "Win Rate", "value": 45, "note": "x" * 5000}},
        ]
        for case in cases:
            with self.subTest(case=case):
                response = self._post(case.get("payload"), raw=case.get("raw"))
                self.assertEqual(response.status_code, 400)
        self.assertFalse(GapMetricMeasurement.objects.exists())

    def test_get_is_not_allowed(self):
        response = self.client.get(self.measure_url, HTTP_AUTHORIZATION=f"Bearer {self.plaintext}")
        self.assertEqual(response.status_code, 405)

    def test_rate_limit_returns_429(self):
        self._make_metric()
        with mock.patch.object(inbound_api, "RATE_LIMIT_MEASUREMENTS", 2):
            self.assertEqual(self._post({"metric": "Win Rate", "value": 31}).status_code, 201)
            self.assertEqual(self._post({"metric": "Win Rate", "value": 32}).status_code, 201)
            response = self._post({"metric": "Win Rate", "value": 33})
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        self.assertEqual(GapMetricMeasurement.objects.count(), 2)


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceHubApiKeyTests(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Hub Keys Co")
        self.other_workspace = Workspace.objects.create(name="Other Keys Co")
        self.users = {}
        for role in ("admin", "funti3r_consultant", "manager", "contributor", "viewer"):
            user = User.objects.create_user(username=f"keys-{role}", password="pass1234", email=f"{role}@keys.test")
            WorkspaceMembership.objects.create(workspace=self.workspace, user=user, role=role, is_active=True)
            self.users[role] = user
        self.url = f"{reverse('workspace_hub')}?workspace={self.workspace.id}"

    def _create(self, role, name="Nightly sync"):
        self.client.force_login(self.users[role])
        return self.client.post(self.url, {"integration_action": "create_api_key", "api_key_name": name})

    def test_admin_creates_key_and_sees_plaintext_once(self):
        response = self._create("admin")
        self.assertEqual(response.status_code, 200)
        api_key = WorkspaceApiKey.objects.get(workspace=self.workspace)
        self.assertEqual(api_key.name, "Nightly sync")
        self.assertEqual(api_key.created_by, self.users["admin"])
        self.assertContains(response, "won't be shown again")
        self.assertIn("no-store", response["Cache-Control"])
        plaintext = response.context["new_api_key"]
        self.assertEqual(WorkspaceApiKey.authenticate(plaintext), api_key)

        followup = self.client.get(self.url)
        self.assertNotContains(followup, plaintext)
        self.assertContains(followup, api_key.prefix)

    def test_consultant_can_create_key(self):
        self._create("funti3r_consultant")
        self.assertTrue(WorkspaceApiKey.objects.filter(workspace=self.workspace).exists())

    def test_other_roles_cannot_create_or_see_keys(self):
        WorkspaceApiKey.generate(self.workspace, "Existing", self.users["admin"])
        for role in ("manager", "contributor", "viewer"):
            with self.subTest(role=role):
                response = self._create(role, name=f"{role} key")
                self.assertNotContains(response, "Existing")
                self.assertFalse(WorkspaceApiKey.objects.filter(name=f"{role} key").exists())

    def test_blank_name_is_rejected(self):
        self._create("admin", name="   ")
        self.assertFalse(WorkspaceApiKey.objects.exists())

    def test_admin_revokes_key(self):
        api_key, plaintext = WorkspaceApiKey.generate(self.workspace, "To revoke", self.users["admin"])
        self.client.force_login(self.users["admin"])
        response = self.client.post(self.url, {"integration_action": "revoke_api_key", "api_key_id": api_key.id})
        self.assertEqual(response.status_code, 200)
        api_key.refresh_from_db()
        self.assertIsNotNone(api_key.revoked_at)
        self.assertIsNone(WorkspaceApiKey.authenticate(plaintext))
        self.assertNotContains(response, "To revoke")

    def test_manager_cannot_revoke_key(self):
        api_key, _ = WorkspaceApiKey.generate(self.workspace, "Keep me", self.users["admin"])
        self.client.force_login(self.users["manager"])
        self.client.post(self.url, {"integration_action": "revoke_api_key", "api_key_id": api_key.id})
        api_key.refresh_from_db()
        self.assertIsNone(api_key.revoked_at)

    def test_malformed_revoke_id_is_handled(self):
        self.client.force_login(self.users["admin"])
        response = self.client.post(self.url, {"integration_action": "revoke_api_key", "api_key_id": "abc"})
        self.assertEqual(response.status_code, 200)

    def test_cannot_revoke_another_workspaces_key(self):
        other_key, _ = WorkspaceApiKey.generate(self.other_workspace, "Not yours", None)
        self.client.force_login(self.users["admin"])
        self.client.post(self.url, {"integration_action": "revoke_api_key", "api_key_id": other_key.id})
        other_key.refresh_from_db()
        self.assertIsNone(other_key.revoked_at)
