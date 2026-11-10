"""Agent-proposed follow-up options: parsing, and that the marker block never
reaches storage or the rendered reply on any of the three chat endpoints."""
import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from gtm.agent_runtime import FOLLOW_UP_MARKER, MAX_FOLLOW_UP_CHARS, split_follow_ups
from gtm.models import AssessmentSession, ChatMessage, WorkspaceChatMessage
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()

REPLY = (
    "Your weakest pillar is **Demand**.\n\n"
    f"{FOLLOW_UP_MARKER}\n"
    "- Turn the top gap into tasks\n"
    "- Show me my scores\n"
)


class SplitFollowUpsTests(SimpleTestCase):
    def test_parses_trailing_block(self):
        text, options = split_follow_ups(REPLY)
        self.assertEqual(text, "Your weakest pillar is **Demand**.")
        self.assertEqual(options, ["Turn the top gap into tasks", "Show me my scores"])

    def test_no_marker_returns_text_unchanged(self):
        self.assertEqual(split_follow_ups("Plain reply."), ("Plain reply.", []))
        self.assertEqual(split_follow_ups(""), ("", []))
        self.assertEqual(split_follow_ups(None), (None, []))

    def test_caps_at_three_and_dedupes(self):
        block = "\n".join(f"- Option {n}" for n in (1, 1, 2, 3, 4))
        _, options = split_follow_ups(f"Hi\n{FOLLOW_UP_MARKER}\n{block}")
        self.assertEqual(options, ["Option 1", "Option 2", "Option 3"])

    def test_overlong_option_is_dropped_not_cut(self):
        long_option = "x" * (MAX_FOLLOW_UP_CHARS + 1)
        _, options = split_follow_ups(f"Hi\n{FOLLOW_UP_MARKER}\n- {long_option}\n- Short one")
        self.assertEqual(options, ["Short one"])

    def test_every_marker_block_is_removed_last_one_wins(self):
        text, options = split_follow_ups(
            f"Intro\n{FOLLOW_UP_MARKER}\n- echoed\nMore prose\n[[Follow-ups]]\n- real"
        )
        self.assertEqual(text, "Intro\nMore prose")
        self.assertEqual(options, ["real"])

    def test_marker_only_text(self):
        self.assertEqual(split_follow_ups(f"{FOLLOW_UP_MARKER}\n- only"), ("", ["only"]))


class FollowUpEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username="fu_user", password="pass1234", email="fu@test.com")
        cls.workspace = Workspace.objects.create(name="Follow Up Co")
        WorkspaceMembership.objects.create(workspace=cls.workspace, user=cls.user, role="admin", is_active=True)
        cls.session = AssessmentSession.objects.create(
            owner_client_id="fu-client", user=cls.user, workspace=cls.workspace,
            company_name="Follow Up Co", is_completed=True,
        )

    def setUp(self):
        self.client.force_login(self.user)
        self.qs = f"?workspace={self.workspace.id}"

    def _post(self, url, payload):
        response = self.client.post(url + self.qs, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def _assert_clean(self, data, stored_response):
        self.assertEqual(data["follow_ups"], ["Turn the top gap into tasks", "Show me my scores"])
        self.assertNotIn("follow-ups", data["response"])
        self.assertNotIn("follow-ups", data["response_html"])
        self.assertNotIn("follow-ups", stored_response)

    @mock.patch("gtm.ai_chat.handle_general_chat", return_value=(REPLY, [], []))
    def test_charlie_endpoint(self, _mock):
        data = self._post(
            reverse("dashboard_agent_api"),
            {"session_id": str(self.session.uuid), "message": "Tell me about our positioning"},
        )
        self._assert_clean(data, ChatMessage.objects.get(session=self.session).response)

    @mock.patch("gtm.workspace_agent_chat.handle_general_chat_workspace", return_value=(REPLY, [], []))
    def test_workspace_agent_endpoint(self, _mock):
        data = self._post(
            reverse("dashboard_workspace_agent_api", args=["gtm_engineer"]),
            {"message": "Tell me about our positioning"},
        )
        self._assert_clean(data, WorkspaceChatMessage.objects.get(workspace=self.workspace).response)

    @mock.patch("gtm.team_chat._run_team_turn", return_value=("gtm_engineer", REPLY, None, None, [], []))
    def test_team_endpoint(self, _mock):
        data = self._post(reverse("dashboard_team_agent_api"), {"message": "Tell me about our positioning"})
        self._assert_clean(data, WorkspaceChatMessage.objects.get(workspace=self.workspace).response)

    def test_agent_hub_renders_starter_prompts_and_attachment_cards(self):
        ChatMessage.objects.create(
            session=self.session, user=self.user, message="Here is our export", response="Thanks",
            attachments=[{"name": "pipeline.csv", "content_type": "text/csv", "size": 2048}],
        )
        response = self.client.get(
            reverse("agent_hub") + self.qs + f"&agent_type=session&agent_session={self.session.uuid}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="agent-starter-prompts"')
        self.assertContains(response, "pipeline.csv")
        self.assertContains(response, ">CSV<")
        self.assertContains(response, 'id="dashboard-agent-input"')
