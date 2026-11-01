"""
Guards the dashboard home view against query-count regressions. Each query is
a round trip to Cloud SQL, so per-day/per-session/per-channel loops add
noticeable latency to the most-visited page. The bound must not grow with the
amount of data in the workspace.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext, override_settings
from django.urls import reverse
from django.utils import timezone

from dashboard.models import Channel, ChannelAnalytics
from gtm.models import (
    ActionItem, AssessmentSession, Category, Question, RecommendationBand,
    Response, ResultSnapshot,
)
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class DashboardQueryCountTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="dash-perf-user", password="pass1234", email="dashperf@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Perf Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.user, role="admin", is_active=True
        )
        RecommendationBand.objects.create(
            min_score=0, max_score=100, stage="Baseline",
            headline="Baseline", actions_markdown="- Keep going",
        )
        categories = [
            Category.objects.create(name=name, weight=1.0)
            for name in ("Demand", "Conversion", "Delivery")
        ]
        questions = [
            Question.objects.create(
                id_code=f"PERF-{i:02d}", category=categories[i % 3],
                text=f"Question {i}", weight=1.0,
            )
            for i in range(18)
        ]
        cls.now = timezone.now()
        cls.categories = categories
        cls.questions = questions

    def setUp(self):
        self.client.force_login(self.user)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def _seed(self, sessions, items, channels):
        # Offset names so a second call doesn't collide with the first.
        base = ActionItem.objects.count() + Channel.objects.count()
        for i in range(sessions):
            s = AssessmentSession.objects.create(
                owner_client_id="perf-client", user=self.user, workspace=self.workspace,
                company_name="Perf Co", is_completed=True,
            )
            AssessmentSession.objects.filter(pk=s.pk).update(
                created_at=self.now - timedelta(days=i)
            )
            Response.objects.bulk_create(
                Response(session=s, question=q, score=1 + (i % 5)) for q in self.questions
            )
            ResultSnapshot.objects.create(
                session=s, overall=40 + i,
                category_breakdown=[{"category": "Demand", "avg": 2.5}],
            )
        for i in range(items):
            ActionItem.objects.create(
                workspace=self.workspace, note=f"Task {base + i}",
                status=("todo", "doing", "done")[i % 3],
            )
        for i in range(channels):
            ch = Channel.objects.create(name=f"Channel {base + i}", color="#000000")
            ChannelAnalytics.objects.create(
                channel=ch, date=self.now.date(), revenue=100 + i, change=1.0
            )

    def _count_dashboard_queries(self):
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        return len(ctx.captured_queries)

    def test_tool_recommendations_are_not_on_the_dashboard(self):
        # Deliberately removed: a static vendor catalog doesn't answer "where
        # are we, what's next". Tool advice lives on the results page,
        # playbook, and Remy's get_tool_recommendations tool instead.
        self._seed(sessions=1, items=1, channels=0)
        response = self.client.get(reverse("dashboard"))
        self.assertNotContains(response, "Top Tool Recommendations")
        self.assertNotIn("top_tool_groups", response.context)

    def test_query_count_does_not_scale_with_data(self):
        self._seed(sessions=2, items=3, channels=1)
        small = self._count_dashboard_queries()
        self._seed(sessions=8, items=30, channels=6)
        large = self._count_dashboard_queries()
        # Was 93 -> 122 before the per-day/per-session/per-channel loops were
        # collapsed; a branch or two may differ with data, growth may not.
        self.assertLessEqual(large, small)
        self.assertLessEqual(small, 55)
