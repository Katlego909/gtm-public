"""Agent-flagged gaps (gtm/agent_actions.py escalate_gap) are drafts a person
accepts, carry a real target and evidence, and can never overwrite or
falsely close a tracked gap. Also covers the display of unmeasured gaps and
the migration that moved earlier untargeted agent rows back to drafts."""

import importlib
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.models import GapAnalysisMetric, GapAnalysisSuggestion, GapMetricMeasurement
from dashboard.utils import calculate_gap_metric_display_properties
from dashboard.views.helpers import record_gap_measurement
from gtm.agent_actions import build_agent_action_tools
from gtm.models import ActionItem, AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()

draft_migration = importlib.import_module("dashboard.migrations.0026_move_untargeted_gaps_to_drafts")


class _GapBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="gap-drafts", password="pass1234", email="gd@test.com")
        cls.workspace = Workspace.objects.create(name="Gap Drafts Co")
        WorkspaceMembership.objects.create(workspace=cls.workspace, user=cls.user, role="admin", is_active=True)
        cls.session = AssessmentSession.objects.create(
            owner_client_id="gap-drafts-client", user=cls.user, workspace=cls.workspace,
            company_name="Gap Drafts Co", is_completed=True,
        )

    def _escalate(self, *args, **kwargs):
        tools = {t.__name__: t for t in build_agent_action_tools(self.workspace, self.user, agent_type="gtm_strategist")}
        return tools["escalate_gap"](*args, **kwargs)

    def _metric(self, **overrides):
        values = dict(
            category="Sales Efficiency", metric="Win Rate", current=25, target=35, priority="High",
            recommendation="Tighten qualification.", workspace=self.workspace, user=self.user,
            session=self.session, source="USER", estimate_method="measured",
        )
        values.update(overrides)
        return GapAnalysisMetric.objects.create(**values)


class EscalateGapTests(_GapBase):
    def test_creates_a_labelled_draft_not_a_tracked_gap(self):
        result = self._escalate(
            "Win Rate", 35, "User's stated goal", "Qualification scored 2/5 (CON-QUA-02).",
            "Enforce qualification fields.",
        )
        self.assertIn("draft", result)
        self.assertFalse(GapAnalysisMetric.objects.exists())
        draft = GapAnalysisSuggestion.objects.get(metric="Win Rate")
        self.assertEqual(draft.category, "Sales Efficiency")  # from the metric, not the agent
        self.assertEqual(draft.session, self.session)
        self.assertEqual(draft.user, self.user)
        self.assertEqual(draft.source_payload["generator"], "agent")
        self.assertEqual(draft.source_payload["proposed_by"], "Charlie")
        self.assertTrue(draft.source_payload["unmeasured"])
        self.assertIn("CON-QUA-02", draft.rationale)
        self.assertIn("User's stated goal", draft.rationale)

    def test_attaches_the_assessment_facts_itself(self):
        from gtm.models import Category, Question, Response

        conversion = Category.objects.create(name="Conversion")
        for code, score in (("CON-QLF-02", 1), ("CON-STG-03", 2), ("CON-SLA-01", 4)):
            question = Question.objects.create(category=conversion, id_code=code, text=code)
            Response.objects.create(session=self.session, question=question, score=score)
        self._escalate("Win Rate", 35, "goal", "the user asked", "x")
        rationale = GapAnalysisSuggestion.objects.get().rationale
        self.assertTrue(rationale.startswith("Assessment: Conversion pillar"))
        self.assertIn("Qualification 1/5", rationale)
        self.assertIn("Agent's evidence: the user asked.", rationale)

    def test_refuses_without_a_real_target_basis_or_evidence(self):
        self.assertIn("target above 0", self._escalate("Win Rate", 0, "goal", "evidence", "x"))
        self.assertIn("target comes from", self._escalate("Win Rate", 35, " ", "evidence", "x"))
        self.assertIn("evidence", self._escalate("Win Rate", 35, "goal", "", "x"))
        self.assertFalse(GapAnalysisSuggestion.objects.exists())

    def test_current_needs_a_source(self):
        self.assertIn("current value came from", self._escalate("Win Rate", 35, "goal", "ev", "x", current=22))
        self._escalate("Win Rate", 35, "goal", "ev", "x", current=22, current_source="deals.csv")
        draft = GapAnalysisSuggestion.objects.get(metric="Win Rate")
        self.assertEqual(draft.current, 22)
        self.assertFalse(draft.source_payload["unmeasured"])

    def test_never_touches_a_tracked_gap(self):
        metric = self._metric()
        result = self._escalate("Win Rate", 50, "goal", "ev", "x")
        self.assertIn("already tracked", result)
        metric.refresh_from_db()
        self.assertEqual((metric.current, metric.target, metric.estimate_method), (25, 35, "measured"))
        self.assertFalse(GapAnalysisSuggestion.objects.exists())

    def test_refuses_a_duplicate_draft(self):
        self._escalate("Win Rate", 35, "goal", "ev", "x")
        self.assertIn("already waiting", self._escalate("Win Rate", 40, "goal", "ev", "x"))
        self.assertEqual(GapAnalysisSuggestion.objects.count(), 1)

    def test_records_an_outcome_for_agent_runs(self):
        sink = []
        tools = {t.__name__: t for t in build_agent_action_tools(self.workspace, self.user, outcomes_sink=sink)}
        tools["escalate_gap"]("Win Rate", 35, "goal", "ev", "x")
        self.assertEqual(sink[0]["tool"], "escalate_gap")
        self.assertEqual(sink[0]["suggestion_id"], GapAnalysisSuggestion.objects.get().id)


@override_settings(SECURE_SSL_REDIRECT=False)
class AcceptAgentDraftTests(_GapBase):
    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def _draft(self, target=35, **payload):
        return GapAnalysisSuggestion.objects.create(
            category="Sales Efficiency", metric="Win Rate", current=0, target=target, priority="Medium",
            recommendation="Enforce qualification fields.", rationale="Qualification scored 2/5.",
            status="pending", session=self.session, workspace=self.workspace, user=self.user,
            source_payload={"generator": "agent", "unmeasured": True, **payload},
        )

    def test_accepting_tracks_it_as_not_measured(self):
        draft = self._draft()
        self.client.post(reverse("accept_gap_suggestion", args=[draft.id]))
        metric = GapAnalysisMetric.objects.get(metric="Win Rate")
        self.assertEqual((metric.target, metric.estimate_method), (35, "unmeasured"))
        draft.refresh_from_db()
        self.assertEqual(draft.status, "accepted")

    def test_a_draft_without_target_needs_one(self):
        draft = self._draft(target=0)
        response = self.client.post(reverse("accept_gap_suggestion", args=[draft.id]))
        self.assertIn("Set a target", response["HX-Trigger"])
        self.assertFalse(GapAnalysisMetric.objects.exists())

        self.client.post(reverse("accept_gap_suggestion", args=[draft.id]), {"target": "40"})
        self.assertEqual(GapAnalysisMetric.objects.get(metric="Win Rate").target, 40)

    def test_panel_asks_for_the_target(self):
        self._draft(target=0)
        response = self.client.get(reverse("refresh_gap_suggestions"))
        self.assertContains(response, 'name="target"')
        self.assertContains(response, "Set target")
        self.assertContains(response, "Not measured")

    def test_accepting_never_overwrites_a_tracked_gap(self):
        metric = self._metric()
        draft = self._draft()
        response = self.client.post(reverse("accept_gap_suggestion", args=[draft.id]))
        self.assertIn("already tracked", response["HX-Trigger"])
        metric.refresh_from_db()
        self.assertEqual((metric.current, metric.estimate_method), (25, "measured"))
        draft.refresh_from_db()
        self.assertEqual(draft.status, "pending")

    def test_regenerating_keeps_agent_drafts(self):
        agent_draft = self._draft()
        generated = GapAnalysisSuggestion.objects.create(
            category="Lead Generation", metric="Monthly Qualified Leads", current=50, target=80,
            priority="High", recommendation="r", status="pending", session=self.session,
            workspace=self.workspace, user=self.user, source_payload={"generator": "gemini"},
        )
        with mock.patch(
            "dashboard.views.gap_analysis._ai_gap_suggestions_from_assessment", return_value=([], {"generator": "gemini"}),
        ):
            self.client.post(reverse("generate_gap_suggestions"))
        self.assertTrue(GapAnalysisSuggestion.objects.filter(id=agent_draft.id).exists())
        self.assertFalse(GapAnalysisSuggestion.objects.filter(id=generated.id).exists())


class UnmeasuredGapTests(_GapBase):
    def test_unmeasured_gap_is_not_shown_as_on_target(self):
        metric = self._metric(current=0, estimate_method="unmeasured")
        calculate_gap_metric_display_properties(metric)
        self.assertEqual(metric.gap_percent, "Awaiting measurement")
        self.assertNotIn("green", metric.gap_class)
        self.assertEqual(metric.estimate_method_label, "Not measured")

    def test_no_target_never_closes_on_a_measurement(self):
        metric = self._metric(current=0, target=0, estimate_method="unmeasured")
        closed = record_gap_measurement(metric, 5, source="manual")
        self.assertFalse(closed)
        metric.refresh_from_db()
        self.assertEqual(metric.status, "open")

    def test_measuring_an_unmeasured_gap_still_closes_at_target(self):
        metric = self._metric(current=0, estimate_method="unmeasured")
        self.assertTrue(record_gap_measurement(metric, 36, source="manual"))


class MoveUntargetedGapsMigrationTests(_GapBase):
    def test_moves_only_untouched_untargeted_agent_rows(self):
        moved = self._metric(metric="Win Rate", current=0, target=0, source="AI", estimate_method="")
        measured = self._metric(metric="Average Deal Size", category="Sales Velocity", current=0, target=0, source="AI")
        GapMetricMeasurement.objects.create(gap_metric=measured, value=4000, source="manual")
        with_task = self._metric(metric="Net Revenue Retention", category="Customer Success", current=0, target=0, source="AI")
        ActionItem.objects.create(workspace=self.workspace, note="Fix onboarding", gap_metric=with_task)
        targeted = self._metric(metric="Monthly Qualified Leads", category="Lead Generation", target=80, source="AI")

        draft_migration.to_drafts(apps, None)

        self.assertFalse(GapAnalysisMetric.objects.filter(id=moved.id).exists())
        self.assertTrue(GapAnalysisMetric.objects.filter(id__in=[measured.id, with_task.id, targeted.id]).count() == 3)
        draft = GapAnalysisSuggestion.objects.get(metric="Win Rate")
        self.assertEqual((draft.status, draft.target, draft.user), ("pending", 0, self.user))
        self.assertTrue(draft.source_payload["migrated"])

        draft_migration.back_to_rows(apps, None)
        self.assertTrue(GapAnalysisMetric.objects.filter(metric="Win Rate", target=0).exists())
        self.assertFalse(GapAnalysisSuggestion.objects.filter(metric="Win Rate").exists())
