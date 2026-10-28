"""Five agents merged into three (Nora into Milo, Theo into Charlie): the
tools moved, every surface lists exactly the three, past work still shows
who wrote it, and live work routed to a retired agent goes to its successor."""

from importlib import import_module

from django.apps import apps
from django.test import TestCase
from django.urls import reverse

from gtm.action_item_completion import completion_agent_type
from gtm.agent_runtime import AGENT_DIRECTORY, agent_display_label, agent_name
from gtm.models import ActionItem, ActionItemComment, AgentRun, WorkspaceChatMessage
from gtm.tests_agent_runs import AgentRunTestBase

ACTIVE = {"gtm_strategist", "insights", "gtm_engineer"}


class ConsolidationTests(AgentRunTestBase):
    def test_exactly_three_agents_everywhere(self):
        from gtm.team_chat import TEAM_AGENT_TYPES
        from gtm.workspace_agent_chat import AGENT_TYPES, SUGGESTED_PROMPTS, SYSTEM_INSTRUCTIONS

        self.assertEqual(set(AGENT_DIRECTORY), ACTIVE)
        self.assertEqual(set(TEAM_AGENT_TYPES), ACTIVE)
        self.assertEqual(set(AGENT_TYPES), {"insights", "gtm_engineer"})
        self.assertEqual(set(SYSTEM_INSTRUCTIONS), {"insights", "gtm_engineer"})
        self.assertEqual(set(SUGGESTED_PROMPTS), {"insights", "gtm_engineer"})

    def test_milo_has_noras_trend_tools(self):
        from gtm.workspace_agent_chat import _build_workspace_tools

        names = {t.__name__ for t in _build_workspace_tools("insights", self.workspace, user=self.admin)}
        for tool in ("get_trend_overview", "get_category_trend", "compare_sessions", "get_assessment_history",
                     "get_score_change", "get_overdue_tasks", "draft_client_summary"):
            self.assertIn(tool, names)
        self.assertNotIn("consult_portfolio_agent", names)
        self.assertNotIn("consult_resource_agent", names)

    def test_charlie_has_theos_library_tools_only_with_a_workspace(self):
        from gtm.ai_chat import _build_session_tools
        from gtm.models import AssessmentSession

        names = {t.__name__ for t in _build_session_tools(self.session, user=self.admin)}
        for tool in ("recommend_resource_for_gap", "get_coverage_gaps", "search_internal_resources",
                     "consult_insights_agent", "consult_gtm_engineer_agent"):
            self.assertIn(tool, names)
        personal = AssessmentSession.objects.create(owner_client_id="personal", user=self.admin)
        names = {t.__name__ for t in _build_session_tools(personal, user=self.admin)}
        self.assertNotIn("recommend_resource_for_gap", names)

    def test_hub_tabs_list_the_three_agents(self):
        http = self.client_class()
        http.force_login(self.admin)
        response = http.get(reverse("agent_hub"), {"workspace": str(self.workspace.id)})
        tabs = [tab[0] for tab in response.context["agent_tabs"]]
        self.assertEqual(tabs, ["session", "insights", "gtm_engineer", "team"])

    def test_past_work_keeps_the_retired_agents_names(self):
        comment = ActionItemComment.objects.create(
            action_item=self.item, user=self.admin, text="Old note", agent_type="resource",
        )
        self.assertEqual(comment.agent_name, "Theo")
        self.assertEqual(agent_name("portfolio"), "Nora")
        self.assertEqual(agent_display_label("portfolio"), "Nora · Portfolio Agent")

    def test_task_routed_to_a_retired_agent_goes_to_its_successor(self):
        self.item.assigned_agent_type = "resource"
        self.assertEqual(completion_agent_type(self.item), "gtm_strategist")
        self.item.assigned_agent_type = "portfolio"
        self.assertEqual(completion_agent_type(self.item), "insights")

    def test_team_chat_continues_with_milo_after_a_nora_turn(self):
        from gtm.team_chat import _resolve_active_agent_type

        WorkspaceChatMessage.objects.create(
            workspace=self.workspace, agent_type="portfolio", user=self.admin, message="trend?", response="Up.",
        )
        self.assertEqual(_resolve_active_agent_type(self.workspace, None), "insights")

    def test_data_migration_redirects_only_live_work(self):
        migration = import_module("gtm.migrations.0060_retire_nora_theo")
        routed = ActionItem.objects.create(
            session=self.session, workspace=self.workspace, note="Routed to Theo", assigned_agent_type="resource",
        )
        open_run = AgentRun.objects.create(
            agent_type="portfolio", goal="x", status="awaiting_approval", requested_by=self.admin, workspace=self.workspace,
        )
        done_run = AgentRun.objects.create(
            agent_type="portfolio", goal="y", status="completed", requested_by=self.admin, workspace=self.workspace,
        )
        migration.redirect_live_work(apps, None)
        routed.refresh_from_db()
        open_run.refresh_from_db()
        done_run.refresh_from_db()
        self.assertEqual(routed.assigned_agent_type, "gtm_strategist")
        self.assertEqual(open_run.agent_type, "insights")
        self.assertEqual(done_run.agent_type, "portfolio")
        self.assertEqual(done_run.agent_name, "Nora")
