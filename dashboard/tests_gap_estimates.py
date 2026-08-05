"""
Tests for the honesty pass on Gap Analysis estimate generation:
- _fallback_gap_suggestions covers all 6 metrics, driven by
  GapAnalysisMetric.CATEGORY_PILLAR_MAPPING, rounded per METRIC_VALUE_GRANULARITY.
- _ai_gap_suggestions_from_assessment grounds its prompt in company context
  and enforces rounding server-side regardless of what Gemini returns.
- estimate_method ('gemini'/'formula'/'manual_entry'/'measured') is carried
  through accept_gap_suggestion, add_edit_gap_metric, and log_gap_measurement
  so the table can show where a number actually came from.
- GapAnalysisMetricForm rejects a current/target pair pointed the wrong way.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.forms import GapAnalysisMetricForm
from dashboard.models import GapAnalysisMetric, GapAnalysisSuggestion
from dashboard.utils import calculate_gap_metric_display_properties
from dashboard.views.helpers import (
    _ai_gap_suggestions_from_assessment,
    _fallback_gap_suggestions,
    _load_pending_gap_suggestions,
)
from gtm.models import AssessmentSession, Category, Question, Response
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


def _make_category_scores(session, pillar_scores):
    """Create a Category + Question + Response per pillar so _compute_scores
    (and therefore _fallback_gap_suggestions/_ai_gap_suggestions_from_assessment)
    has real data to work from, matching the given {pillar_name: score} map."""
    for pillar, score in pillar_scores.items():
        category = Category.objects.create(name=pillar, weight=1.0)
        question = Question.objects.create(
            id_code=f"{pillar[:3].upper()}-TST-01", category=category,
            text=f"How mature is your {pillar} motion?", weight=1.0,
        )
        Response.objects.create(session=session, question=question, score=score)


class FallbackGapSuggestionsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fallback-est", password="pass1234", email="f@test.com")
        self.session = AssessmentSession.objects.create(
            owner_client_id="fallback-est-client", user=self.user, company_name="Fallback Co",
        )
        _make_category_scores(self.session, {"Demand": 2, "Conversion": 4, "Delivery": 3})

    def _compute(self):
        from gtm.services import _compute_scores
        cat_scores, _overall = _compute_scores(self.session)
        return cat_scores

    def test_covers_all_six_metrics(self):
        suggestions = _fallback_gap_suggestions(self._compute())
        metrics = {s["metric"] for s in suggestions}
        self.assertEqual(metrics, set(GapAnalysisMetric.METRIC_FIELD_MAPPING.keys()))

    def test_values_rounded_per_metric_granularity(self):
        suggestions = _fallback_gap_suggestions(self._compute())
        for item in suggestions:
            step = GapAnalysisMetric.METRIC_VALUE_GRANULARITY[item["metric"]]
            for field in ("current", "target"):
                value = item[field]
                # value / step must land on (approximately) a whole multiple
                multiple = value / step
                self.assertAlmostEqual(multiple, round(multiple), places=6)

    def test_metrics_sharing_a_pillar_move_together(self):
        """Monthly Qualified Leads and CAC Payback Period both key off the
        Demand pillar (only 3 real pillars drive 6 metrics) -- confirms the
        mapping is actually being used, not an incidental accident."""
        suggestions = {s["metric"]: s for s in _fallback_gap_suggestions(self._compute())}
        low_demand = suggestions["Monthly Qualified Leads"]["priority"]
        # Both driven by the same (low) Demand score of 2 -> both High priority.
        self.assertEqual(low_demand, "High")
        self.assertEqual(suggestions["CAC Payback Period"]["priority"], "High")

    def test_higher_is_better_metrics_have_target_above_current(self):
        suggestions = {s["metric"]: s for s in _fallback_gap_suggestions(self._compute())}
        for metric in ("Monthly Qualified Leads", "Product Qualified Leads", "Win Rate",
                       "Average Deal Size", "Net Revenue Retention"):
            self.assertGreater(suggestions[metric]["target"], suggestions[metric]["current"])
        self.assertLess(suggestions["CAC Payback Period"]["target"], suggestions["CAC Payback Period"]["current"])

    def test_empty_scores_returns_empty(self):
        self.assertEqual(_fallback_gap_suggestions([]), [])


@override_settings(SECURE_SSL_REDIRECT=False)
class AIGapSuggestionsPromptTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ai-est", password="pass1234", email="ai@test.com")
        self.session = AssessmentSession.objects.create(
            owner_client_id="ai-est-client", user=self.user,
            company_name="Acme Robotics", industry="Robotics", company_size="11-50",
            company_stage="growth",
        )
        _make_category_scores(self.session, {"Demand": 2, "Conversion": 3, "Delivery": 4})

    def _mock_client(self, response_text):
        mock_client = mock.Mock()
        usage = mock.Mock(total_token_count=42)
        mock_client.models.generate_content.return_value = mock.Mock(text=response_text, usage_metadata=usage)
        return mock_client

    def test_prompt_includes_company_context(self):
        mock_client = self._mock_client('{"suggestions": []}')
        with mock.patch("gtm.ai_services._quota_cooldown_active", return_value=False), \
             mock.patch("gtm.ai_services._request_budget_available", return_value=True), \
             mock.patch("gtm.ai_services._get_client", return_value=mock_client), \
             mock.patch("gtm.ai_credits.record_spend"):
            _ai_gap_suggestions_from_assessment(self.session)

        prompt = mock_client.models.generate_content.call_args.kwargs.get("contents") \
            or mock_client.models.generate_content.call_args.args[-1]
        self.assertIn("Acme Robotics", prompt)
        self.assertIn("Robotics", prompt)
        self.assertIn("11-50", prompt)

    def test_ai_response_values_get_rounded_server_side(self):
        response_json = (
            '{"suggestions": [{"category": "Lead Generation", "metric": "Monthly Qualified Leads", '
            '"current": 96.37, "target": 114.29, "priority": "High", '
            '"recommendation": "Tighten ICP filters.", "rationale": "Demand scored low.", "confidence": 80}]}'
        )
        mock_client = self._mock_client(response_json)
        with mock.patch("gtm.ai_services._quota_cooldown_active", return_value=False), \
             mock.patch("gtm.ai_services._request_budget_available", return_value=True), \
             mock.patch("gtm.ai_services._get_client", return_value=mock_client), \
             mock.patch("gtm.ai_credits.record_spend"):
            suggestions, metadata = _ai_gap_suggestions_from_assessment(self.session)

        self.assertEqual(metadata["generator"], "gemini")
        self.assertEqual(suggestions[0]["current"], 96.0)
        self.assertEqual(suggestions[0]["target"], 114.0)

    def test_falls_back_when_ai_unavailable(self):
        with mock.patch("gtm.ai_services._quota_cooldown_active", return_value=True):
            suggestions, metadata = _ai_gap_suggestions_from_assessment(self.session)

        self.assertEqual(metadata["generator"], "fallback")
        self.assertTrue(suggestions)


@override_settings(SECURE_SSL_REDIRECT=False)
class EstimateMethodProvenanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="prov-est", password="pass1234", email="prov@test.com")
        cls.workspace = Workspace.objects.create(name="Provenance Co")
        WorkspaceMembership.objects.create(workspace=cls.workspace, user=cls.user, role="admin", is_active=True)
        cls.session = AssessmentSession.objects.create(
            owner_client_id="prov-est-client", user=cls.user, workspace=cls.workspace,
            company_name="Provenance Co", is_completed=True,
        )

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def _make_suggestion(self, generator):
        return GapAnalysisSuggestion.objects.create(
            category="Sales Efficiency", metric="Win Rate", current=30, target=40,
            priority="High", recommendation="Improve qualification.", rationale="Low conversion score.",
            confidence=75, status="pending", session=self.session, workspace=self.workspace,
            user=self.user, source_payload={"generator": generator},
        )

    def test_accept_gemini_suggestion_sets_estimate_method_gemini(self):
        suggestion = self._make_suggestion("gemini")
        self.client.post(reverse("accept_gap_suggestion", args=[suggestion.id]))
        metric = GapAnalysisMetric.objects.get(workspace=self.workspace, metric="Win Rate")
        self.assertEqual(metric.estimate_method, "gemini")

    def test_accept_fallback_suggestion_sets_estimate_method_formula(self):
        suggestion = self._make_suggestion("fallback_after_ai_error")
        self.client.post(reverse("accept_gap_suggestion", args=[suggestion.id]))
        metric = GapAnalysisMetric.objects.get(workspace=self.workspace, metric="Win Rate")
        self.assertEqual(metric.estimate_method, "formula")

    def test_manual_add_sets_estimate_method_manual_entry(self):
        response = self.client.post(reverse("add_gap_metric"), {
            "category": "Sales Efficiency", "metric": "Win Rate",
            "current": 25, "target": 35, "priority": "High",
            "recommendation": "Manual entry test.",
        })
        self.assertNotEqual(response.status_code, 500)
        metric = GapAnalysisMetric.objects.get(workspace=self.workspace, metric="Win Rate")
        self.assertEqual(metric.estimate_method, "manual_entry")

    def test_log_measurement_sets_estimate_method_measured(self):
        metric = GapAnalysisMetric.objects.create(
            category="Sales Efficiency", metric="Win Rate", current=30, target=40,
            priority="High", recommendation="x", source="AI", estimate_method="gemini",
            session=self.session, workspace=self.workspace, user=self.user,
        )
        self.client.post(reverse("log_gap_measurement", args=[metric.id]), {"value": "32"})
        metric.refresh_from_db()
        self.assertEqual(metric.estimate_method, "measured")


class GapAnalysisMetricFormValidationTests(TestCase):
    def _form_data(self, **overrides):
        data = dict(
            category="Marketing ROI", metric="CAC Payback Period",
            current=10, target=12, priority="High", recommendation="x",
        )
        data.update(overrides)
        return data

    def test_rejects_cac_payback_with_target_above_current(self):
        form = GapAnalysisMetricForm(data=self._form_data(current=10, target=12))
        self.assertFalse(form.is_valid())
        self.assertIn("target", form.errors)

    def test_accepts_cac_payback_with_target_below_current(self):
        form = GapAnalysisMetricForm(data=self._form_data(current=12, target=10))
        self.assertTrue(form.is_valid(), form.errors)

    def test_rejects_higher_is_better_metric_with_target_below_current(self):
        form = GapAnalysisMetricForm(data=self._form_data(
            category="Sales Efficiency", metric="Win Rate", current=40, target=30,
        ))
        self.assertFalse(form.is_valid())
        self.assertIn("target", form.errors)

    def test_accepts_higher_is_better_metric_with_target_above_current(self):
        form = GapAnalysisMetricForm(data=self._form_data(
            category="Sales Efficiency", metric="Win Rate", current=30, target=40,
        ))
        self.assertTrue(form.is_valid(), form.errors)


class PseudoMetricDisplayPropertiesTests(TestCase):
    """Regression test: calculate_gap_metric_display_properties is called on
    two different shapes of object -- a real GapAnalysisMetric (has
    estimate_method) and the suggestion panel's lightweight PseudoMetric
    stand-in (does not). Adding estimate_method-based badging must not
    require every caller's object to have that attribute."""

    def test_calculate_display_properties_on_object_without_estimate_method(self):
        pseudo_metric = type('PseudoMetric', (), {
            'metric': 'Win Rate', 'current': 30, 'target': 40, 'priority': 'High',
        })()
        calculate_gap_metric_display_properties(pseudo_metric)
        self.assertIsNone(pseudo_metric.estimate_method_label)
        self.assertIsNone(pseudo_metric.estimate_method_class)
        self.assertEqual(pseudo_metric.gap_percent, "-25.0%")

    def test_load_pending_gap_suggestions_does_not_crash(self):
        from django.test import RequestFactory

        user = User.objects.create_user(username="pseudo-metric", password="pass1234", email="pseudo@test.com")
        GapAnalysisSuggestion.objects.create(
            category="Sales Efficiency", metric="Win Rate", current=30, target=40,
            priority="High", recommendation="x", status="pending", user=user,
            source_payload={"generator": "gemini"},
        )
        request = RequestFactory().get("/dashboard/")
        request.user = user

        suggestions = _load_pending_gap_suggestions(request, None)

        self.assertEqual(len(suggestions), 1)
        self.assertIsNotNone(suggestions[0].gap_percent)
