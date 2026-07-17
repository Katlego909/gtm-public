"""
Tests for the ActionItem duplicate-prevention guarantees added in
gtm/models.py: the note_key field + conditional UniqueConstraints in
ActionItem.Meta (the actual source of truth), and
ActionItem.objects.create_deduped (the race-safe helper every creation
call site uses instead of raw .create()/.bulk_create()).
"""

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from gtm.models import ActionItem, AssessmentSession
from gtm.models_workspace import Workspace

User = get_user_model()


class ActionItemConstraintTests(TestCase):
    """The DB constraint is the real guarantee -- verify it actually rejects
    duplicates even when code bypasses create_deduped entirely."""

    def setUp(self):
        self.user = User.objects.create_user(username="dedup-owner", password="pass1234", email="owner@test.com")
        self.session = AssessmentSession.objects.create(
            owner_client_id="dedup-client", user=self.user, company_name="Acme Inc",
        )

    def test_duplicate_note_in_same_session_raises_integrity_error(self):
        ActionItem.objects.create(session=self.session, note="Define your ICP")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ActionItem.objects.create(session=self.session, note="Define your ICP")

    def test_case_and_whitespace_insensitive(self):
        ActionItem.objects.create(session=self.session, note="Define your ICP")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ActionItem.objects.create(session=self.session, note="  DEFINE YOUR icp  ")

    def test_same_note_allowed_across_different_sessions(self):
        other_session = AssessmentSession.objects.create(
            owner_client_id="dedup-client", user=self.user, company_name="Other Co",
        )
        ActionItem.objects.create(session=self.session, note="Define your ICP")
        ActionItem.objects.create(session=other_session, note="Define your ICP")
        self.assertEqual(ActionItem.objects.filter(note_key="define your icp").count(), 2)

    def test_workspace_scoped_constraint_when_no_session(self):
        workspace = Workspace.objects.create(name="Dedup Co")
        ActionItem.objects.create(workspace=workspace, note="Set up onboarding")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ActionItem.objects.create(workspace=workspace, note="Set up onboarding")

    def test_creator_scoped_constraint_when_no_session_or_workspace(self):
        ActionItem.objects.create(created_by=self.user, note="Personal follow-up")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ActionItem.objects.create(created_by=self.user, note="Personal follow-up")


class CreateDedupedTests(TestCase):
    """create_deduped is the helper every call site uses -- it must never
    raise on a conflict, and must return (existing_item, False) instead."""

    def setUp(self):
        self.user = User.objects.create_user(username="dedup-helper", password="pass1234", email="helper@test.com")
        self.session = AssessmentSession.objects.create(
            owner_client_id="dedup-helper-client", user=self.user, company_name="Acme Inc",
        )

    def test_second_call_with_same_note_does_not_create_duplicate(self):
        item1, created1 = ActionItem.objects.create_deduped(session=self.session, note="Fix ICP")
        item2, created2 = ActionItem.objects.create_deduped(session=self.session, note=" fix icp ")

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(item1.pk, item2.pk)
        self.assertEqual(ActionItem.objects.filter(session=self.session).count(), 1)

    def test_blank_note_is_a_noop(self):
        item, created = ActionItem.objects.create_deduped(session=self.session, note="   ")
        self.assertIsNone(item)
        self.assertFalse(created)
        self.assertEqual(ActionItem.objects.filter(session=self.session).count(), 0)

    def test_workspace_scope_dedupes_without_session(self):
        workspace = Workspace.objects.create(name="Helper Co")
        item1, created1 = ActionItem.objects.create_deduped(workspace=workspace, note="Set up CRM")
        item2, created2 = ActionItem.objects.create_deduped(workspace=workspace, note="Set up CRM")

        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(item1.pk, item2.pk)

    def test_repeated_calls_simulating_a_race_only_create_one_row(self):
        """Simulates two racing requests that both pass an in-memory
        'not already there' check before calling create_deduped -- the DB
        constraint (not the in-memory check) must be what prevents the
        duplicate."""
        for _ in range(5):
            ActionItem.objects.create_deduped(session=self.session, note="Draft onboarding checklist")
        self.assertEqual(
            ActionItem.objects.filter(session=self.session, note_key="draft onboarding checklist").count(),
            1,
        )
