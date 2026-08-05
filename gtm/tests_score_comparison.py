from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from gtm.models import (
    AssessmentSession,
    Category,
    Question,
    RecommendationBand,
    Response,
    ResultSnapshot,
)
from gtm.models_workspace import Workspace
from gtm.score_comparison import build_score_comparison


class ScoreComparisonTestsBase(TestCase):
    """Shared fixtures: real Question id_codes (matching scoring_engine's
    QUESTION_META) so pattern-detection triggers fire exactly as they would
    in production, not the generic Q1/Q2 placeholders used by tests that
    don't care about pattern content."""

    DEMAND_CODES = [
        "DEM-ICP-01", "DEM-FIT-02", "DEM-MSG-03",
        "DEM-CHN-04", "DEM-ATT-05", "DEM-CNT-06",
    ]
    CONVERSION_CODES = [
        "CON-SLA-01", "CON-QLF-02", "CON-STG-03",
        "CON-OBJ-04", "CON-WNL-05", "CON-PGE-06",
    ]
    DELIVERY_CODES = [
        "DEL-TTV-01", "DEL-ONB-02", "DEL-HLT-03",
        "DEL-RET-04", "DEL-QBR-05", "DEL-ADV-06",
    ]

    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="compareuser", email="compare@example.com", password="pass1234",
        )
        self.band_low = RecommendationBand.objects.create(
            min_score=0, max_score=50, stage="Early Traction", headline="Getting started",
        )
        self.band_high = RecommendationBand.objects.create(
            min_score=51, max_score=100, stage="Scaling Ready", headline="Ready to scale",
        )
        self.categories = {
            "Demand": Category.objects.create(name="Demand", weight=1.0),
            "Conversion": Category.objects.create(name="Conversion", weight=1.0),
            "Delivery": Category.objects.create(name="Delivery", weight=1.0),
        }
        all_codes = self.DEMAND_CODES + self.CONVERSION_CODES + self.DELIVERY_CODES
        cat_for_code = (
            {c: "Demand" for c in self.DEMAND_CODES}
            | {c: "Conversion" for c in self.CONVERSION_CODES}
            | {c: "Delivery" for c in self.DELIVERY_CODES}
        )
        Question.objects.bulk_create([
            Question(
                id_code=code, category=self.categories[cat_for_code[code]],
                text=f"Question {code}", weight=1.0, diagnostic_note="",
            )
            for code in all_codes
        ])
        self.questions = {q.id_code: q for q in Question.objects.filter(id_code__in=all_codes)}

    def _make_session(self, *, company_name, created_at, workspace=None, user=None,
                       is_completed=True, response_scores=None, snapshot_kwargs=None):
        session = AssessmentSession.objects.create(
            owner_client_id="test-client", user=user, workspace=workspace,
            company_name=company_name, is_completed=is_completed,
        )
        # created_at is auto_now_add -- force it after creation so tests can
        # control chronological ordering between sessions.
        AssessmentSession.objects.filter(pk=session.pk).update(created_at=created_at)
        session.refresh_from_db()

        if response_scores:
            Response.objects.bulk_create([
                Response(session=session, question=self.questions[code], score=score)
                for code, score in response_scores.items()
            ])

        if snapshot_kwargs is not None:
            ResultSnapshot.objects.create(
                session=session,
                overall=snapshot_kwargs.get("overall", 50.0),
                band=snapshot_kwargs.get("band"),
                band_stage=snapshot_kwargs.get("band_stage", ""),
                band_headline=snapshot_kwargs.get("band_headline", ""),
                category_breakdown=snapshot_kwargs.get("category_breakdown", []),
                radar_labels=snapshot_kwargs.get("radar_labels", ["Demand", "Conversion", "Delivery"]),
                radar_values=snapshot_kwargs.get("radar_values", [0, 0, 0]),
            )
        return session


class NoPriorAssessmentTests(ScoreComparisonTestsBase):
    def test_first_ever_assessment_returns_none(self):
        now = timezone.now()
        session = self._make_session(
            company_name="Acme Corp", created_at=now, user=self.user,
            snapshot_kwargs={"overall": 60.0, "category_breakdown": [{"category": "Demand", "avg": 3.0}]},
        )
        self.assertIsNone(build_score_comparison(session))

    def test_current_session_without_snapshot_returns_none(self):
        now = timezone.now()
        session = self._make_session(
            company_name="Acme Corp", created_at=now, user=self.user, snapshot_kwargs=None,
        )
        self.assertIsNone(build_score_comparison(session))


class WorkspaceComparisonTests(ScoreComparisonTestsBase):
    def setUp(self):
        super().setUp()
        self.workspace = Workspace.objects.create(name="Acme Workspace")

    def test_computes_overall_and_category_deltas(self):
        now = timezone.now()
        self._make_session(
            company_name="Acme Corp", created_at=now - timedelta(days=30), workspace=self.workspace,
            snapshot_kwargs={
                "overall": 55.0, "band": self.band_low, "band_stage": "Early Traction",
                "category_breakdown": [
                    {"category": "Demand", "avg": 3.0},
                    {"category": "Conversion", "avg": 2.0},
                    {"category": "Delivery", "avg": 4.0},
                ],
            },
        )
        current = self._make_session(
            company_name="Acme Corp", created_at=now, workspace=self.workspace,
            snapshot_kwargs={
                "overall": 70.0, "band": self.band_high, "band_stage": "Scaling Ready",
                "radar_labels": ["Demand", "Conversion", "Delivery"],
                "category_breakdown": [
                    {"category": "Demand", "avg": 4.0},
                    {"category": "Conversion", "avg": 3.0},
                    {"category": "Delivery", "avg": 4.0},
                ],
            },
        )

        comparison = build_score_comparison(current)
        self.assertIsNotNone(comparison)
        self.assertEqual(comparison["overall_delta"], 15.0)
        self.assertTrue(comparison["band_changed"])
        self.assertEqual(comparison["previous_band_stage"], "Early Traction")

        deltas_by_cat = {row["category"]: row for row in comparison["category_deltas"]}
        self.assertEqual(deltas_by_cat["Demand"]["delta"], 1.0)
        self.assertEqual(deltas_by_cat["Conversion"]["delta"], 1.0)
        self.assertEqual(deltas_by_cat["Delivery"]["delta"], 0.0)
        self.assertEqual(comparison["previous_radar_values"], [3.0, 2.0, 4.0])

    def test_pattern_diff_newly_triggered_and_resolved(self):
        now = timezone.now()
        # Previous: Demand & Conversion both healthy (no CONVERSION_LEAK);
        # DEL-RET-04 <= 2 fires DELIVERY_CHURN_RISK.
        previous_scores = {c: 4 for c in self.DEMAND_CODES}
        previous_scores.update({c: 4 for c in self.CONVERSION_CODES})
        previous_scores.update({c: 4 for c in self.DELIVERY_CODES})
        previous_scores["DEL-RET-04"] = 1
        self._make_session(
            company_name="Acme Corp", created_at=now - timedelta(days=30), workspace=self.workspace,
            response_scores=previous_scores,
            snapshot_kwargs={"overall": 50.0, "category_breakdown": [{"category": "Demand", "avg": 4.0}]},
        )

        # Current: Demand still healthy (>=3.0), Conversion craters (<2.5)
        # -> fires CONVERSION_LEAK; Delivery healthy again -> resolves
        # DELIVERY_CHURN_RISK.
        current_scores = {c: 4 for c in self.DEMAND_CODES}
        current_scores.update({c: 1 for c in self.CONVERSION_CODES})
        current_scores.update({c: 4 for c in self.DELIVERY_CODES})
        current = self._make_session(
            company_name="Acme Corp", created_at=now, workspace=self.workspace,
            response_scores=current_scores,
            snapshot_kwargs={"overall": 45.0, "category_breakdown": [{"category": "Demand", "avg": 4.0}]},
        )

        comparison = build_score_comparison(current)
        self.assertIsNotNone(comparison)

        newly_triggered_names = {p["name"] for p in comparison["newly_triggered_patterns"]}
        resolved_names = {p["name"] for p in comparison["resolved_patterns"]}
        self.assertIn("CONVERSION_LEAK", newly_triggered_names)
        self.assertIn("DELIVERY_CHURN_RISK", resolved_names)
        self.assertNotIn("CONVERSION_LEAK", resolved_names)
        self.assertNotIn("DELIVERY_CHURN_RISK", newly_triggered_names)

    def test_future_session_is_not_treated_as_previous(self):
        now = timezone.now()
        current = self._make_session(
            company_name="Acme Corp", created_at=now, workspace=self.workspace,
            snapshot_kwargs={"overall": 60.0, "category_breakdown": [{"category": "Demand", "avg": 3.0}]},
        )
        # Started later but happens to be looked at "after" -- created_at is
        # in the future relative to `current`, so it must NOT count as "last time".
        self._make_session(
            company_name="Acme Corp", created_at=now + timedelta(days=1), workspace=self.workspace,
            snapshot_kwargs={"overall": 90.0, "category_breakdown": [{"category": "Demand", "avg": 5.0}]},
        )
        self.assertIsNone(build_score_comparison(current))

    def test_category_only_in_current_has_no_numeric_delta(self):
        now = timezone.now()
        self._make_session(
            company_name="Acme Corp", created_at=now - timedelta(days=10), workspace=self.workspace,
            snapshot_kwargs={
                "overall": 40.0,
                "category_breakdown": [{"category": "Demand", "avg": 3.0}],  # stage-exempt: no Conversion/Delivery yet
            },
        )
        current = self._make_session(
            company_name="Acme Corp", created_at=now, workspace=self.workspace,
            snapshot_kwargs={
                "overall": 60.0,
                "category_breakdown": [
                    {"category": "Demand", "avg": 4.0},
                    {"category": "Conversion", "avg": 3.0},
                ],
            },
        )
        comparison = build_score_comparison(current)
        deltas_by_cat = {row["category"]: row for row in comparison["category_deltas"]}
        self.assertEqual(deltas_by_cat["Demand"]["delta"], 1.0)
        self.assertIsNone(deltas_by_cat["Conversion"]["delta"])
        self.assertIsNone(deltas_by_cat["Conversion"]["previous"])
        self.assertEqual(deltas_by_cat["Conversion"]["current"], 3.0)

    def test_query_count_is_bounded_not_linear_in_history_size(self):
        now = timezone.now()
        for i in range(10):
            self._make_session(
                company_name="Acme Corp", created_at=now - timedelta(days=30 - i), workspace=self.workspace,
                snapshot_kwargs={"overall": 40.0 + i, "category_breakdown": [{"category": "Demand", "avg": 3.0}]},
            )
        current = self._make_session(
            company_name="Acme Corp", created_at=now, workspace=self.workspace,
            snapshot_kwargs={"overall": 60.0, "category_breakdown": [{"category": "Demand", "avg": 4.0}]},
        )
        with CaptureQueriesContext(connection) as ctx:
            comparison = build_score_comparison(current)
        self.assertIsNotNone(comparison)
        # One query to find+fetch the previous session (select_related folds
        # its snapshot in), one for its Response rows -- bounded regardless
        # of how many older sessions exist in the workspace.
        self.assertLessEqual(len(ctx), 4)


class SoloUserComparisonTests(ScoreComparisonTestsBase):
    def test_matching_company_name_case_and_whitespace_insensitive(self):
        now = timezone.now()
        self._make_session(
            company_name="Acme Corp", created_at=now - timedelta(days=5), user=self.user,
            snapshot_kwargs={"overall": 40.0, "category_breakdown": [{"category": "Demand", "avg": 3.0}]},
        )
        current = self._make_session(
            company_name="  acme corp  ", created_at=now, user=self.user,
            snapshot_kwargs={"overall": 55.0, "category_breakdown": [{"category": "Demand", "avg": 4.0}]},
        )
        comparison = build_score_comparison(current)
        self.assertIsNotNone(comparison)
        self.assertEqual(comparison["overall_delta"], 15.0)

    def test_different_company_name_returns_none(self):
        now = timezone.now()
        self._make_session(
            company_name="Acme Corp", created_at=now - timedelta(days=5), user=self.user,
            snapshot_kwargs={"overall": 40.0, "category_breakdown": [{"category": "Demand", "avg": 3.0}]},
        )
        current = self._make_session(
            company_name="Globex Inc", created_at=now, user=self.user,
            snapshot_kwargs={"overall": 55.0, "category_breakdown": [{"category": "Demand", "avg": 4.0}]},
        )
        self.assertIsNone(build_score_comparison(current))

    def test_blank_company_name_returns_none(self):
        now = timezone.now()
        self._make_session(
            company_name="", created_at=now - timedelta(days=5), user=self.user,
            snapshot_kwargs={"overall": 40.0, "category_breakdown": [{"category": "Demand", "avg": 3.0}]},
        )
        current = self._make_session(
            company_name="", created_at=now, user=self.user,
            snapshot_kwargs={"overall": 55.0, "category_breakdown": [{"category": "Demand", "avg": 4.0}]},
        )
        self.assertIsNone(build_score_comparison(current))
