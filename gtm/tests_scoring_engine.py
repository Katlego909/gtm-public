"""
Tests for gtm/scoring_engine.py's opportunity points (the results page's
Priority Opportunities card): each item must equal the real change in the
overall score if that question reached 5/5, the items must sum to
100 - overall even for partial / stage-exempt assessments, and ties must
not be decided by seed order.

Score sets are Ardent SA's real assessments (5 Jun: all 18 answered;
11 Jul: Demand only).
"""

from django.test import SimpleTestCase, TestCase

from gtm.models import AssessmentSession, Category, Question, Response
from gtm.scoring_engine import (
    PILLAR_WEIGHTS,
    QUESTION_META,
    build_recommendation_context,
    compute_opportunity_scores,
    compute_overall,
    compute_pillar_avgs,
)
from gtm.services import _compute_scores

FULL_JUN_05 = {
    "DEM-ICP-01": 3, "DEM-FIT-02": 2, "DEM-MSG-03": 4, "DEM-CHN-04": 2, "DEM-ATT-05": 4, "DEM-CNT-06": 2,
    "CON-SLA-01": 2, "CON-QLF-02": 4, "CON-STG-03": 2, "CON-OBJ-04": 4, "CON-WNL-05": 4, "CON-PGE-06": 2,
    "DEL-TTV-01": 2, "DEL-ONB-02": 4, "DEL-HLT-03": 3, "DEL-RET-04": 4, "DEL-QBR-05": 4, "DEL-ADV-06": 3,
}

DEMAND_ONLY_JUL_11 = {
    "DEM-ICP-01": 1, "DEM-FIT-02": 2, "DEM-MSG-03": 2, "DEM-CHN-04": 3, "DEM-ATT-05": 1, "DEM-CNT-06": 2,
}


class OpportunityPointsTests(SimpleTestCase):
    def _assert_items_are_exact_overall_deltas(self, scores):
        current = compute_overall(scores)
        for opp in compute_opportunity_scores(scores):
            raised = compute_overall({**scores, opp["id_code"]: 5})
            # compute_overall rounds to 0.1, so allow that much slack.
            self.assertAlmostEqual(raised - current, opp["opportunity_pts"], delta=0.1, msg=opp["dimension"])

    def test_full_assessment_matches_screenshot_and_sums_to_remaining_points(self):
        ctx = build_recommendation_context(FULL_JUN_05)
        self.assertEqual(ctx["current_overall"], 60.0)
        self.assertEqual(ctx["total_opportunity_pts"], 40.0)
        pipeline = next(o for o in ctx["all_opportunities"] if o["id_code"] == "CON-STG-03")
        self.assertEqual(pipeline["opportunity_pts"], 5.28)
        self._assert_items_are_exact_overall_deltas(FULL_JUN_05)

    def test_demand_only_assessment_counts_points_against_answered_pillars(self):
        ctx = build_recommendation_context(DEMAND_ONLY_JUL_11)
        self.assertEqual(ctx["current_overall"], 36.8)
        # Was 25.3 before: per-item points ignored the answered-pillar renormalization.
        self.assertEqual(ctx["total_opportunity_pts"], 63.2)
        icp = next(o for o in ctx["all_opportunities"] if o["id_code"] == "DEM-ICP-01")
        self.assertEqual(icp["opportunity_pts"], 17.6)  # 4 × 0.22 × (0.4 / 0.4) × 20
        self._assert_items_are_exact_overall_deltas(DEMAND_ONLY_JUL_11)

    def test_partially_answered_pillar_still_sums_to_remaining_points(self):
        scores = {"DEM-ICP-01": 2, "DEM-CHN-04": 3, "CON-STG-03": 1}
        ctx = build_recommendation_context(scores)
        self.assertAlmostEqual(ctx["total_opportunity_pts"] + ctx["current_overall"], 100.0, delta=0.1)
        self._assert_items_are_exact_overall_deltas(scores)

    def test_tie_goes_to_weaker_pillar_not_seed_order(self):
        # Lead Quality (Demand) and Funnel Optimization (Conversion) both
        # score 2 with weight 0.16 in 0.4-weight pillars: worth 3.84 each.
        scores = dict(FULL_JUN_05)
        avgs = compute_pillar_avgs(scores)
        self.assertLess(avgs["Demand"], avgs["Conversion"])
        ranked = [o["id_code"] for o in compute_opportunity_scores(scores)]
        self.assertLess(ranked.index("DEM-FIT-02"), ranked.index("CON-PGE-06"))

        # Make Conversion the weaker pillar without touching the tied pair:
        # the order must flip.
        scores.update({"CON-QLF-02": 1, "CON-WNL-05": 1, "DEM-MSG-03": 5, "DEM-ATT-05": 5})
        avgs = compute_pillar_avgs(scores)
        self.assertLess(avgs["Conversion"], avgs["Demand"])
        ranked = [o["id_code"] for o in compute_opportunity_scores(scores)]
        self.assertLess(ranked.index("CON-PGE-06"), ranked.index("DEM-FIT-02"))

    def test_ordering_is_independent_of_input_order(self):
        reversed_scores = dict(reversed(list(FULL_JUN_05.items())))
        self.assertEqual(
            [o["id_code"] for o in compute_opportunity_scores(FULL_JUN_05)],
            [o["id_code"] for o in compute_opportunity_scores(reversed_scores)],
        )


class EngineMatchesDatabaseScoringTests(TestCase):
    """compute_overall is the in-memory twin of services._compute_scores;
    the opportunity math is only right if the two agree."""

    def setUp(self):
        categories = {name: Category.objects.create(name=name, weight=w) for name, w in PILLAR_WEIGHTS.items()}
        self.questions = {
            code: Question.objects.create(id_code=code, category=categories[meta["pillar"]], text=code, weight=meta["weight"])
            for code, meta in QUESTION_META.items()
        }

    def _overall_from_db(self, scores):
        session = AssessmentSession.objects.create(owner_client_id="engine-db")
        for code, score in scores.items():
            Response.objects.create(session=session, question=self.questions[code], score=score)
        return round(_compute_scores(session)[1], 1)

    def test_full_and_partial_assessments_agree(self):
        for scores in (FULL_JUN_05, DEMAND_ONLY_JUL_11, {"DEM-ICP-01": 2, "CON-STG-03": 1}):
            with self.subTest(answers=len(scores)):
                self.assertEqual(self._overall_from_db(scores), compute_overall(scores))


class TopPrioritiesTests(SimpleTestCase):
    def _top(self, scores):
        return [(o["dimension"], o["pillar"]) for o in build_recommendation_context(scores)["top_priorities"]]

    def test_one_priority_per_pillar_ordered_by_points(self):
        # Pure impact order would be Pipeline Hygiene, Channel Strategy, Lead Quality (Demand twice).
        self.assertEqual(self._top(FULL_JUN_05), [
            ("Pipeline Hygiene", "Conversion"),
            ("Channel Strategy", "Demand"),
            ("Time to Value", "Delivery"),
        ])

    def test_single_pillar_assessment_still_fills_three_slots(self):
        top = self._top(DEMAND_ONLY_JUL_11)
        self.assertEqual(len(top), 3)
        self.assertEqual([d for d, _ in top], ["ICP Clarity", "Attribution", "Positioning"])

    def test_total_still_counts_every_question(self):
        ctx = build_recommendation_context(FULL_JUN_05)
        self.assertEqual(ctx["total_opportunity_pts"], 40.0)
        self.assertEqual(len(ctx["all_opportunities"]), 18)


class TopPrioritiesSummaryTests(SimpleTestCase):
    def test_summary_is_the_shown_items_not_every_question(self):
        ctx = build_recommendation_context(FULL_JUN_05)
        # As displayed: Pipeline Hygiene 5.3 + Channel Strategy 4.8 + Time to Value 2.2
        self.assertEqual(ctx["top_priorities_pts"], 12.3)
        self.assertEqual(ctx["top_priorities_target"], 72.3)
        self.assertEqual(ctx["total_opportunity_pts"], 40.0)
