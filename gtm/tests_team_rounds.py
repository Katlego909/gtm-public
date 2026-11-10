from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.models import Notification
from gtm.models import AssessmentSession, Category, Question, Response
from gtm.models_team_rounds import TeamRound
from gtm.models_workspace import Workspace, WorkspaceMembership
from gtm.services import _is_session_complete
from gtm.team_rounds import RoundNotReady, close_round, open_round, question_summaries, submit_answers

User = get_user_model()


class TeamRoundSetup(TestCase):
    def setUp(self):
        self.workspace = Workspace.objects.create(name="Team Co", industry="SaaS")
        self.admin = self._member("admin_user", "admin")
        self.sales = self._member("sales_user", "contributor")
        self.cs = self._member("cs_user", "contributor")
        self.viewer = self._member("viewer_user", "viewer")
        self.questions = []
        for name in ("Demand", "Conversion"):
            category = Category.objects.create(name=name)
            for i in range(2):
                self.questions.append(Question.objects.create(
                    id_code=f"{name[:3].upper()}-{i}", category=category, text=f"{name} question {i}"))

    def _member(self, username, role):
        user = User.objects.create_user(username=username, password="pass1234")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=user, role=role, is_active=True)
        return user

    def _answer(self, team_round, user, scores, notes=None):
        respondent = team_round.respondents.get(user=user)
        notes = notes or {}
        submit_answers(respondent, {q.id: (s, notes.get(i, "")) for i, (q, s) in enumerate(zip(self.questions, scores))})


class TeamRoundRulesTests(TeamRoundSetup):
    def test_close_needs_two_submissions(self):
        team_round = open_round(self.workspace, self.admin, "Q4", [self.sales, self.cs])
        self._answer(team_round, self.sales, [4, 4, 4, 4])
        with self.assertRaises(RoundNotReady):
            close_round(team_round, self.admin)

    def test_consensus_is_rounded_average_and_flags_disagreement(self):
        team_round = open_round(self.workspace, self.admin, "Q4", [self.sales, self.cs])
        self._answer(team_round, self.sales, [4, 3, 5, 2], notes={2: "We track this weekly."})
        self._answer(team_round, self.cs, [2, 4, 5, 2])

        summaries = question_summaries(team_round)
        self.assertEqual([s["consensus"] for s in summaries], [3, 4, 5, 2])  # 3.5 rounds half up to 4
        self.assertEqual([s["flagged"] for s in summaries], [True, False, False, False])

        session = close_round(team_round, self.admin)
        team_round.refresh_from_db()
        self.assertEqual(team_round.status, "closed")
        self.assertEqual(team_round.consensus_session, session)
        self.assertTrue(session.is_completed)
        self.assertEqual(session.workspace, self.workspace)
        self.assertEqual(session.company_name, "Team Co")
        self.assertTrue(_is_session_complete(session))
        responses = {r.question_id: r for r in Response.objects.filter(session=session)}
        self.assertEqual(responses[self.questions[0].id].score, 3)
        self.assertIn("Team disagreement", responses[self.questions[0].id].context_note)
        self.assertIn("We track this weekly.", responses[self.questions[2].id].context_note)
        self.assertNotIn("sales_user", responses[self.questions[2].id].context_note)

    def test_cannot_close_twice(self):
        team_round = open_round(self.workspace, self.admin, "Q4", [self.sales, self.cs])
        self._answer(team_round, self.sales, [3, 3, 3, 3])
        self._answer(team_round, self.cs, [3, 3, 3, 3])
        close_round(team_round, self.admin)
        with self.assertRaises(RoundNotReady):
            close_round(team_round, self.admin)
        self.assertEqual(AssessmentSession.objects.filter(workspace=self.workspace).count(), 1)

    def test_stage_and_firmographics_come_from_latest_assessment(self):
        AssessmentSession.objects.create(owner_client_id="x", workspace=self.workspace, is_completed=True,
                                         company_name="Team Co Inc", company_stage="idea", country="ZA")
        team_round = open_round(self.workspace, self.admin, "Q4", [self.sales, self.cs])
        self.assertEqual(team_round.company_stage, "idea")
        self.assertEqual({s["question"].category.name for s in question_summaries(team_round)}, {"Demand"})


@override_settings(SECURE_SSL_REDIRECT=False)
class TeamRoundViewTests(TeamRoundSetup):
    def test_admin_opens_round_and_respondents_are_notified(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("team_rounds", args=[self.workspace.id]), {
            "title": "Q4 check", "respondents": [self.sales.id, self.cs.id]})
        team_round = TeamRound.objects.get()
        self.assertRedirects(response, reverse("team_round_detail", args=[self.workspace.id, team_round.pk]),
                             fetch_redirect_response=False)
        self.assertEqual(set(team_round.respondents.values_list("user_id", flat=True)), {self.sales.id, self.cs.id})
        self.assertEqual(Notification.objects.filter(recipient__in=[self.sales, self.cs]).count(), 2)

    def test_contributor_cannot_open_round(self):
        self.client.force_login(self.sales)
        response = self.client.post(reverse("team_rounds", args=[self.workspace.id]), {
            "respondents": [self.sales.id, self.cs.id]})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(TeamRound.objects.exists())

    def test_non_member_is_denied(self):
        outsider = User.objects.create_user(username="outsider", password="x")
        self.client.force_login(outsider)
        self.assertEqual(self.client.get(reverse("team_rounds", args=[self.workspace.id])).status_code, 403)

    def test_answers_hidden_while_open_and_shown_after_close(self):
        team_round = open_round(self.workspace, self.admin, "Q4", [self.sales, self.cs])
        self.client.force_login(self.sales)
        answer_url = reverse("team_round_answer", args=[self.workspace.id, team_round.pk])
        self.client.post(answer_url, {f"q_{q.id}": "4" for q in self.questions})
        self._answer(team_round, self.cs, [1, 4, 4, 4])

        detail_url = reverse("team_round_detail", args=[self.workspace.id, team_round.pk])
        self.client.force_login(self.viewer)
        self.assertNotContains(self.client.get(detail_url), "Disagreement")

        self.client.force_login(self.admin)
        response = self.client.post(reverse("team_round_close", args=[self.workspace.id, team_round.pk]))
        team_round.refresh_from_db()
        self.assertRedirects(response, reverse("gtm:results", args=[team_round.consensus_session.uuid]),
                             fetch_redirect_response=False)
        self.client.force_login(self.viewer)
        page = self.client.get(detail_url)
        self.assertContains(page, "Disagreement: 3 points apart")
        self.assertContains(page, "sales_user: 4")

    def test_incomplete_answers_are_rejected(self):
        team_round = open_round(self.workspace, self.admin, "Q4", [self.sales, self.cs])
        self.client.force_login(self.sales)
        response = self.client.post(reverse("team_round_answer", args=[self.workspace.id, team_round.pk]),
                                    {f"q_{self.questions[0].id}": "4"})
        self.assertContains(response, "Please score every question")
        self.assertIsNone(team_round.respondents.get(user=self.sales).submitted_at)

    def test_non_respondent_cannot_answer(self):
        team_round = open_round(self.workspace, self.admin, "Q4", [self.sales, self.cs])
        self.client.force_login(self.viewer)
        response = self.client.get(reverse("team_round_answer", args=[self.workspace.id, team_round.pk]))
        self.assertEqual(response.status_code, 302)

    def test_contributor_cannot_close(self):
        team_round = open_round(self.workspace, self.admin, "Q4", [self.sales, self.cs])
        self.client.force_login(self.sales)
        response = self.client.post(reverse("team_round_close", args=[self.workspace.id, team_round.pk]))
        self.assertEqual(response.status_code, 403)
