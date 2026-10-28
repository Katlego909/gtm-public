"""
Tests that each agent's tools reflect what it actually is: Charlie's
priorities come from the deterministic scoring engine (not canned text), and
"Complete with AI" on a task routed to a workspace agent runs as that agent,
with its own domain tools. Tool closures are called directly without a live
Gemini call, matching gtm/tests_agent_actions.py.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from gtm.action_item_completion import _build_completion_tools, completion_agent_type
from gtm.agent_runs import _persona
from gtm.ai_chat import _build_session_tools
from gtm.models import ActionItem, AgentDocument, AgentRun, AssessmentSession, Category, Question, Response
from gtm.models_workspace import Workspace, WorkspaceMembership
from gtm.scoring_engine import build_recommendation_context
from gtm.tests_scoring_engine import FULL_JUN_05

User = get_user_model()

PILLAR_BY_PREFIX = {"DEM": "Demand", "CON": "Conversion", "DEL": "Delivery"}


class CharlieDiagnosisToolTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.session = AssessmentSession.objects.create(owner_client_id="diag-client", company_name="Diag Co")
        categories = {name: Category.objects.create(name=name) for name in PILLAR_BY_PREFIX.values()}
        for id_code, score in FULL_JUN_05.items():
            question = Question.objects.create(
                id_code=id_code, category=categories[PILLAR_BY_PREFIX[id_code[:3]]], text=id_code,
            )
            Response.objects.create(session=cls.session, question=question, score=score)

    def _tools(self, session=None):
        return {t.__name__: t for t in _build_session_tools(session or self.session)}

    def test_canned_strategy_tools_are_gone(self):
        names = set(self._tools())
        self.assertIn("get_gtm_diagnosis", names)
        for removed in (
            "competitive_benchmarking_analysis", "customer_segment_analysis", "resource_allocation_guidance",
            "analyze_risk_and_mitigation", "build_implementation_roadmap",
        ):
            self.assertNotIn(removed, names)

    def test_diagnosis_matches_scoring_engine_priorities_and_patterns(self):
        ctx = build_recommendation_context(FULL_JUN_05)
        result = self._tools()["get_gtm_diagnosis"]()

        self.assertIn(f"Overall score: {ctx['current_overall']}/100", result)
        self.assertIn(f"worth {ctx['top_priorities_pts']} points together", result)
        for opp in ctx["top_priorities"]:
            self.assertIn(f"{opp['dimension']} ({opp['pillar']}", result)
            self.assertIn(f"+{round(opp['opportunity_pts'], 1)} points", result)
        for pat in ctx["patterns"]:
            self.assertIn(pat["name"], result)

    def test_diagnosis_without_answers_says_so(self):
        empty = AssessmentSession.objects.create(owner_client_id="diag-empty")
        result = self._tools(empty)["get_gtm_diagnosis"]()
        self.assertIn("no answered questions", result)


class CompletionAgentRoutingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="routing-user", password="pass1234")
        cls.workspace = Workspace.objects.create(name="Routing Co")
        WorkspaceMembership.objects.create(workspace=cls.workspace, user=cls.user, role="admin", is_active=True)
        cls.session = AssessmentSession.objects.create(
            owner_client_id="routing-client", user=cls.user, workspace=cls.workspace, company_name="Routing Co",
        )

    def _item(self, assigned_agent_type=None):
        return ActionItem.objects.create(
            session=self.session, workspace=self.workspace, note="Pick a sales deck for the ICP gap",
            status="todo", assigned_agent_type=assigned_agent_type,
        )

    def test_unrouted_task_is_completed_by_charlie(self):
        item = self._item()
        self.assertEqual(completion_agent_type(item), "gtm_strategist")
        names = {t.__name__ for t in _build_completion_tools(item, self.user, [])}
        self.assertIn("get_gtm_diagnosis", names)
        self.assertNotIn("find_resource", names)

    def test_task_routed_to_milo_runs_as_milo_with_his_tools(self):
        item = self._item("insights")
        agent_type = completion_agent_type(item)
        self.assertEqual(agent_type, "insights")

        created = []
        tools = {t.__name__: t for t in _build_completion_tools(item, self.user, created, agent_type=agent_type)}
        for milo_tool in ("get_trend_overview", "compare_sessions", "get_overdue_tasks", "get_pending_reviews"):
            self.assertIn(milo_tool, tools)
        self.assertNotIn("draft_client_summary", tools)

        tools["create_document"]("Trend brief", "Demand rose from 2.1 to 2.6.")
        self.assertEqual(AgentDocument.objects.get(pk=created[0]).agent_type, "insights")

        run = AgentRun(agent_type=agent_type, goal=item.note, session=self.session, workspace=self.workspace)
        self.assertIn("You are Milo", _persona(run))
