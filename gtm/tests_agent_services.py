"""
Tests for gtm/agent_services.py::build_execution_plan -- the "build my
action plan" chat command. Its persist step used to be a plain
ActionItem.objects.create() per suggested task, guarded only by an
in-memory note-text snapshot taken at the top of the function (a
check-then-act race). It now goes through ActionItem.objects.create_deduped,
so these tests lock in that created_items/skipped_items still reflect what
actually landed in the DB.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from gtm.agent_services import build_execution_plan
from gtm.models import ActionItem, AssessmentSession, Category, Question, Response

User = get_user_model()


class BuildExecutionPlanDedupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="plan-builder", password="pass1234", email="plan-builder@test.com"
        )
        cls.category = Category.objects.create(name="Demand", weight=1.0)
        cls.question = Question.objects.create(
            id_code="DEM-TEST-01",
            category=cls.category,
            text="How consistently do you generate new pipeline?",
            weight=1.0,
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="plan-builder-client", user=cls.user, company_name="Plan Co",
        )
        Response.objects.create(session=cls.session, question=cls.question, score=1)

    def test_skips_task_that_already_exists_for_the_session(self):
        """The exact task build_execution_plan would derive from the low
        score already exists (e.g. added manually) -- it must be reported
        as skipped, not created a second time."""
        existing_note = "Launch one repeatable weekly demand-generation activity and track lead volume."
        ActionItem.objects.create(session=self.session, note=existing_note)

        result = build_execution_plan(self.session, actor=self.user, persist=True)

        self.assertEqual(result["created_count"], 0)
        self.assertIn(existing_note, result["skipped_items"])
        self.assertEqual(
            ActionItem.objects.filter(session=self.session, note_key=existing_note.lower()).count(), 1
        )

    def test_creates_task_when_none_exists_yet(self):
        result = build_execution_plan(self.session, actor=self.user, persist=True)

        self.assertEqual(result["created_count"], 1)
        self.assertEqual(ActionItem.objects.filter(session=self.session).count(), 1)

    def test_calling_twice_never_produces_a_duplicate_row(self):
        build_execution_plan(self.session, actor=self.user, persist=True)
        build_execution_plan(self.session, actor=self.user, persist=True)

        notes = list(ActionItem.objects.filter(session=self.session).values_list("note_key", flat=True))
        self.assertEqual(len(notes), len(set(notes)), f"duplicate note_key found in {notes}")
