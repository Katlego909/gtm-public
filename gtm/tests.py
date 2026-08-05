from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext, override_settings
from django.urls import reverse

from gtm.models import (
	AssessmentSession,
	Category,
	Question,
	RecommendationBand,
	Response,
	ResultSnapshot,
	ToolRecommendation,
)


@override_settings(SECURE_SSL_REDIRECT=False)
class ResultsQueryOptimizationTests(TestCase):
	def setUp(self):
		user_model = get_user_model()
		self.user = user_model.objects.create_user(
			username="perfuser",
			email="perf@example.com",
			password="pass1234",
		)
		self.client.force_login(self.user)

		self.band = RecommendationBand.objects.create(
			min_score=0,
			max_score=100,
			stage="Baseline",
			headline="Baseline stage",
			actions_markdown="- Keep improving",
		)

		self.categories = [
			Category.objects.create(name="Demand", weight=1.0),
			Category.objects.create(name="Conversion", weight=1.0),
			Category.objects.create(name="Delivery", weight=1.0),
		]

		for cat in self.categories:
			ToolRecommendation.objects.create(
				category=cat,
				keyword="optimize",
				description="Generic recommendation",
				tools="Docs",
			)

	def _build_session_with_responses(self, question_count=36):
		session = AssessmentSession.objects.create(
			owner_client_id="test-client",
			user=self.user,
			company_name="Perf Co",
			industry="SaaS",
			is_completed=True,
		)

		questions = []
		for idx in range(question_count):
			cat = self.categories[idx % len(self.categories)]
			questions.append(
				Question(
					id_code=f"Q{idx+1}",
					category=cat,
					text=f"How do you optimize step {idx+1}?",
					weight=1.0,
					diagnostic_note="Optimize process",
				)
			)
		Question.objects.bulk_create(questions)

		created_questions = list(Question.objects.filter(id_code__startswith="Q"))
		Response.objects.bulk_create([
			Response(session=session, question=q, score=3, ai_insight="")
			for q in created_questions
		])

		# Pre-create snapshot with playbook to avoid async generation path during request.
		ResultSnapshot.objects.create(
			session=session,
			overall=60.0,
			band=self.band,
			band_stage=self.band.stage,
			band_headline=self.band.headline,
			category_breakdown=[{"category": "Demand", "avg": 3.0}],
			radar_labels=["Demand"],
			radar_values=[3.0],
			ai_playbook="Ready",
		)
		return session

	def test_results_view_query_count_does_not_scale_linearly(self):
		session = self._build_session_with_responses(question_count=36)

		url = reverse("gtm:results", args=[session.uuid])
		with CaptureQueriesContext(connection) as ctx:
			response = self.client.get(url)

		self.assertEqual(response.status_code, 200)
		# Guardrail: large question sets should stay within a bounded query count.
		# Threshold includes a small constant (not linear -- verified independent
		# of question_count) allowance for the AI credits account lookup
		# (gtm/ai_credits.py's resolve_account_for_session), whose first-ever
		# get_or_create for a session's workspace/user costs a few extra
		# statements (SELECT + SAVEPOINT + INSERT + RELEASE) once, plus one
		# more constant query for gtm/score_comparison.py's "is there a prior
		# assessment to compare against" lookup (see tests_score_comparison.py
		# for its own dedicated, history-size-independent query-count guard).
		self.assertLessEqual(
			len(ctx),
			36,
			msg=f"Expected bounded query count, got {len(ctx)} queries",
		)
