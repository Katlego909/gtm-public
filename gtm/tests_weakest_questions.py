"""
Regression tests for gtm/views/results.py's `weakest_questions` computation.

Root cause being guarded against: `Question.objects.all()` has no `order_by`,
so ties in `score * question.weight` were silently broken by primary-key
order -- since Demand's questions were seeded with the lowest PKs, every tie
resolved in Demand's favor, letting it appear twice while shutting Delivery
out of the panel entirely, even when Delivery's weakest question was just as
weak. The fix groups by category first and takes one weakest question per
category, guaranteeing every answered pillar is represented.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models import (
    AssessmentSession,
    Category,
    Question,
    RecommendationBand,
    Response,
    ResultSnapshot,
)

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class WeakestQuestionsTieBreakTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="weakest-q-user", password="pass1234", email="wq@test.com"
        )
        cls.band = RecommendationBand.objects.create(
            min_score=0, max_score=100, stage="Baseline", headline="Baseline stage",
            actions_markdown="- Keep improving",
        )
        cls.demand = Category.objects.create(name="Demand", weight=1.0)
        cls.conversion = Category.objects.create(name="Conversion", weight=1.0)
        cls.delivery = Category.objects.create(name="Delivery", weight=1.0)

    def _make_snapshot(self, session):
        ResultSnapshot.objects.create(
            session=session, overall=60.0, band=self.band,
            band_stage=self.band.stage, band_headline=self.band.headline,
            category_breakdown=[{"category": "Demand", "avg": 3.0}],
            radar_labels=["Demand"], radar_values=[3.0], ai_playbook="Ready",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_all_three_pillars_represented_despite_ties(self):
        """Demand has two questions tied for weakest, Conversion and Delivery
        each have one at the exact same weighted score -- under the old
        PK-order tie-break this would pick Demand, Demand, Conversion and
        never reach Delivery. The fix must show all 3 categories."""
        session = AssessmentSession.objects.create(
            owner_client_id="wq-client", user=self.user, company_name="Tie Co",
            is_completed=True,
        )
        # Two tied-weakest Demand questions (reproduces "Demand picked twice"),
        # plus one stronger Demand question that should NOT be selected.
        dem_weak_1 = Question.objects.create(id_code="DEM-A", category=self.demand, text="d1", weight=1.0)
        dem_weak_2 = Question.objects.create(id_code="DEM-B", category=self.demand, text="d2", weight=1.0)
        dem_strong = Question.objects.create(id_code="DEM-C", category=self.demand, text="d3", weight=1.0)
        con_weak = Question.objects.create(id_code="CON-A", category=self.conversion, text="c1", weight=1.0)
        con_strong = Question.objects.create(id_code="CON-B", category=self.conversion, text="c2", weight=1.0)
        del_weak = Question.objects.create(id_code="DEL-A", category=self.delivery, text="dl1", weight=1.0)
        del_strong = Question.objects.create(id_code="DEL-B", category=self.delivery, text="dl2", weight=1.0)

        for q, score in [
            (dem_weak_1, 2), (dem_weak_2, 2), (dem_strong, 4),
            (con_weak, 2), (con_strong, 4),
            (del_weak, 2), (del_strong, 4),
        ]:
            Response.objects.create(session=session, question=q, score=score)

        self._make_snapshot(session)

        response = self.client.get(reverse("gtm:results", args=[session.uuid]))
        self.assertEqual(response.status_code, 200)

        weakest = response.context["weakest_questions"]
        self.assertEqual(len(weakest), 3)
        category_names = sorted(row["question"].category.name for row in weakest)
        self.assertEqual(category_names, ["Conversion", "Delivery", "Demand"])

    def test_category_with_no_responses_is_simply_omitted(self):
        """A company-stage-exempted pillar with zero answered questions must
        not crash the grouping -- it's just absent from the result."""
        session = AssessmentSession.objects.create(
            owner_client_id="wq-client-2", user=self.user, company_name="Partial Co",
            is_completed=True,
        )
        dem_q = Question.objects.create(id_code="DEM-X", category=self.demand, text="d1", weight=1.0)
        con_q = Question.objects.create(id_code="CON-X", category=self.conversion, text="c1", weight=1.0)
        # No Delivery question answered at all.
        Response.objects.create(session=session, question=dem_q, score=2)
        Response.objects.create(session=session, question=con_q, score=3)

        self._make_snapshot(session)

        response = self.client.get(reverse("gtm:results", args=[session.uuid]))
        self.assertEqual(response.status_code, 200)

        weakest = response.context["weakest_questions"]
        category_names = sorted(row["question"].category.name for row in weakest)
        self.assertEqual(category_names, ["Conversion", "Demand"])
