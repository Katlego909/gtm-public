"""
Tests for how task comments render on the Tasks board
(dashboard/partials/action_item_comment.html): agent-written comments are
attributed to the agent, long comments open on a whole-sentence preview,
and older comments fold away.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.agent_actions import build_agent_action_tools
from gtm.models import ActionItem, ActionItemComment
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()

LONG_TEXT = (
    "I have created a document titled MQL Criteria to CRM Data Point Mapping. "
    "It explains how the defined MQL criteria map to data points in the CRM. "
    "The document covers firmographic, demographic and behavioral data. "
    "It also lists the explicit fields that need to be added."
)


class CommentModelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="comment-model", password="pass1234")
        self.item = ActionItem.objects.create(note="Map MQL criteria")

    def _comment(self, text, **kwargs):
        return ActionItemComment.objects.create(action_item=self.item, user=self.user, text=text, **kwargs)

    def test_short_comment_has_no_preview(self):
        self.assertEqual(self._comment("Looks good.").preview, "")

    def test_long_comment_preview_ends_on_a_whole_sentence(self):
        preview = self._comment(LONG_TEXT).preview
        self.assertTrue(preview)
        self.assertTrue(preview.endswith("."))
        self.assertTrue(LONG_TEXT.startswith(preview))
        self.assertLess(len(preview), len(LONG_TEXT))

    def test_preview_ignores_markdown_syntax(self):
        preview = self._comment("**Done.** " + LONG_TEXT).preview
        self.assertNotIn("**", preview)

    def test_agent_name(self):
        self.assertEqual(self._comment("x", agent_type="portfolio").agent_name, "Nora")
        self.assertEqual(self._comment("x").agent_name, "")


class AgentCommentToolTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="comment-tool", password="pass1234")
        self.workspace = Workspace.objects.create(name="Comment Tool Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.user, role="admin", is_active=True)
        self.item = ActionItem.objects.create(workspace=self.workspace, note="Map MQL criteria")

    def test_comment_on_task_records_which_agent_wrote_it(self):
        tools = {t.__name__: t for t in build_agent_action_tools(self.workspace, self.user, agent_type="insights")}
        tools["comment_on_task"](str(self.item.id), "Found a blocker.")
        comment = ActionItemComment.objects.get(action_item=self.item)
        self.assertEqual(comment.agent_type, "insights")
        self.assertEqual(comment.user, self.user)


@override_settings(SECURE_SSL_REDIRECT=False)
class CommentRenderingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="comment-render", password="pass1234")
        self.workspace = Workspace.objects.create(name="Comment Render Co")
        WorkspaceMembership.objects.create(workspace=self.workspace, user=self.user, role="admin", is_active=True)
        self.item = ActionItem.objects.create(workspace=self.workspace, note="Map MQL criteria")
        self.client.force_login(self.user)

    def _board(self):
        response = self.client.get(reverse("tasks_board"), {"workspace": str(self.workspace.id)})
        self.assertEqual(response.status_code, 200)
        return response

    def test_ai_comment_is_attributed_to_the_agent(self):
        ActionItemComment.objects.create(
            action_item=self.item, user=self.user, agent_type="gtm_strategist",
            text="AI completed this task: done.",
        )
        response = self._board()
        self.assertContains(response, ">Charlie</span>")
        self.assertContains(response, "AI &middot; for comment-render")

    def test_long_comment_shows_preview_and_full_text(self):
        ActionItemComment.objects.create(action_item=self.item, user=self.user, text=LONG_TEXT)
        response = self._board()
        self.assertContains(response, "Show more")
        self.assertContains(response, "It also lists the explicit fields that need to be added.")
        self.assertNotContains(response, "max-h-32")

    def test_older_comments_fold_away(self):
        for n in range(4):
            ActionItemComment.objects.create(action_item=self.item, user=self.user, text=f"Comment {n}.")
        response = self._board()
        self.assertContains(response, "Show 2 earlier comments")
        for n in range(4):
            self.assertContains(response, f"Comment {n}.")

    def test_comment_markdown_is_sanitized(self):
        ActionItemComment.objects.create(
            action_item=self.item, user=self.user, text="**Bold** <script>alert(1)</script>",
        )
        response = self._board()
        self.assertContains(response, "<strong>Bold</strong>")
        self.assertNotContains(response, "<script>alert(1)</script>")
