import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.models import GapAnalysisMetric, Resource
from gtm.account_deletion import (
    AccountDeletionBlocked, delete_account, delete_workspace, workspaces_blocking_deletion,
)
from gtm.models import ActionItem, AssessmentSession, ChatMessage, DeliveryDocument
from gtm.models_workspace import Workspace, WorkspaceMembership

User = get_user_model()
TEMP_MEDIA = tempfile.mkdtemp()


def _member(workspace, user, role):
    return WorkspaceMembership.objects.create(workspace=workspace, user=user, role=role, is_active=True)


def _gap(**kwargs):
    return GapAnalysisMetric.objects.create(
        category="Lead Generation", metric="Win Rate", target=30, priority="High",
        recommendation="r", **kwargs)


class AccountDeletionRulesTests(TestCase):
    def setUp(self):
        self.leaver = User.objects.create_user(username="leaver", password="pass1234")
        self.teammate = User.objects.create_user(username="teammate", password="pass1234")
        self.shared = Workspace.objects.create(name="Shared Co")
        _member(self.shared, self.leaver, "admin")
        _member(self.shared, self.teammate, "contributor")

    def test_sole_admin_of_shared_workspace_is_blocked_and_nothing_is_deleted(self):
        with self.assertRaises(AccountDeletionBlocked) as ctx:
            delete_account(self.leaver)
        self.assertEqual(ctx.exception.workspaces, [self.shared])
        self.assertTrue(User.objects.filter(pk=self.leaver.pk).exists())

    def test_inactive_teammates_do_not_block(self):
        WorkspaceMembership.objects.filter(user=self.teammate).update(is_active=False)
        self.assertEqual(workspaces_blocking_deletion(self.leaver), [])

    def test_with_another_admin_shared_data_survives_detached(self):
        WorkspaceMembership.objects.filter(user=self.teammate).update(role="admin")
        shared_session = AssessmentSession.objects.create(
            owner_client_id="c", user=self.leaver, workspace=self.shared, is_completed=True)
        task = ActionItem.objects.create(note="keep me", session=shared_session, workspace=self.shared)
        shared_gap = _gap(user=self.leaver, workspace=self.shared)
        personal_session = AssessmentSession.objects.create(owner_client_id="c", user=self.leaver)

        delete_account(self.leaver)

        self.assertFalse(User.objects.filter(pk=self.leaver.pk).exists())
        self.assertTrue(Workspace.objects.filter(pk=self.shared.pk).exists())
        shared_session.refresh_from_db()
        self.assertIsNone(shared_session.user)
        self.assertTrue(ActionItem.objects.filter(pk=task.pk).exists())
        shared_gap.refresh_from_db()
        self.assertIsNone(shared_gap.user)
        self.assertFalse(AssessmentSession.objects.filter(pk=personal_session.pk).exists())

    def test_sole_member_workspace_is_deleted_with_the_account(self):
        WorkspaceMembership.objects.filter(user=self.teammate).update(role="admin")
        solo = Workspace.objects.create(name="Solo Co")
        _member(solo, self.leaver, "admin")
        solo_session = AssessmentSession.objects.create(owner_client_id="c", user=self.leaver, workspace=solo)

        delete_account(self.leaver)

        self.assertFalse(Workspace.objects.filter(pk=solo.pk).exists())
        self.assertFalse(AssessmentSession.objects.filter(pk=solo_session.pk).exists())


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class StorageCleanupTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.user = User.objects.create_user(username="uploader", password="pass1234")
        self.session = AssessmentSession.objects.create(owner_client_id="c", user=self.user)

    def _doc(self):
        doc = DeliveryDocument(session=self.session, original_filename="deck.pdf")
        doc.file.save("deck.pdf", ContentFile(b"%PDF-1.4"), save=True)
        return doc

    def test_deleting_a_row_removes_its_file_after_commit(self):
        doc = self._doc()
        name = doc.file.name
        self.assertTrue(default_storage.exists(name))
        with self.captureOnCommitCallbacks(execute=True):
            doc.delete()
        self.assertFalse(default_storage.exists(name))

    def test_cascade_from_account_deletion_removes_files_and_chat_attachments(self):
        name = self._doc().file.name
        attachment = default_storage.save("agent_uploads/note.txt", ContentFile(b"hi"))
        ChatMessage.objects.create(session=self.session, user=self.user, message="m", response="r",
                                   attachments=[{"name": "note.txt", "path": attachment}])
        with self.captureOnCommitCallbacks(execute=True):
            delete_account(self.user)
        self.assertFalse(default_storage.exists(name))
        self.assertFalse(default_storage.exists(attachment))

    def test_workspace_deletion_removes_resource_files(self):
        workspace = Workspace.objects.create(name="Files Co")
        resource = Resource(workspace=workspace, name="Playbook", resource_type="file")
        resource.file.save("playbook.pdf", ContentFile(b"x"), save=True)
        name = resource.file.name
        with self.captureOnCommitCallbacks(execute=True):
            delete_workspace(workspace)
        self.assertFalse(default_storage.exists(name))

    def test_replacing_a_resource_file_removes_the_old_one(self):
        workspace = Workspace.objects.create(name="Swap Co")
        resource = Resource(workspace=workspace, name="Deck", resource_type="file")
        resource.file.save("old.pdf", ContentFile(b"old"), save=True)
        old_name = resource.file.name
        with self.captureOnCommitCallbacks(execute=True):
            resource.file.save("new.pdf", ContentFile(b"new"), save=True)
        self.assertFalse(default_storage.exists(old_name))
        self.assertTrue(default_storage.exists(resource.file.name))

    def test_rolled_back_delete_keeps_the_file(self):
        doc = self._doc()
        name = doc.file.name
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            doc.delete()
        self.assertEqual(len(callbacks), 1)  # queued, not run: no commit happened
        self.assertTrue(default_storage.exists(name))


@override_settings(SECURE_SSL_REDIRECT=False)
class DeleteAccountViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="viewer", password="pass1234", email="v@test.com")
        self.url = reverse("delete_account")

    def test_wrong_password_keeps_account(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"password": "nope"})
        self.assertContains(response, "not correct")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_correct_password_deletes_and_logs_out(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"password": "pass1234"})
        self.assertContains(response, "Your account has been deleted")
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_blocked_user_sees_workspace_and_is_not_deleted(self):
        workspace = Workspace.objects.create(name="Needs Admin Co")
        _member(workspace, self.user, "admin")
        _member(workspace, User.objects.create_user(username="other", password="x"), "viewer")
        self.client.force_login(self.user)
        self.assertContains(self.client.get(self.url), "Needs Admin Co")
        response = self.client.post(self.url, {"password": "pass1234"})
        self.assertContains(response, "needs another admin")
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())

    def test_passwordless_account_confirms_with_email(self):
        self.user.set_unusable_password()
        self.user.save()
        self.client.force_login(self.user)
        self.client.post(self.url, {"confirm_identity": "wrong@test.com"})
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.client.post(self.url, {"confirm_identity": "V@test.com"})
        self.assertFalse(User.objects.filter(pk=self.user.pk).exists())

    def test_anonymous_redirected(self):
        self.assertEqual(self.client.get(self.url).status_code, 302)


class PurgeStaleDataCommandTests(TestCase):
    def setUp(self):
        from io import StringIO
        self.out = StringIO()
        # Own media dir per test: files left by another test would be real orphans.
        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=media)
        override.enable()
        self.addCleanup(override.disable)
        session = AssessmentSession.objects.create(owner_client_id="c")
        self.kept = DeliveryDocument(session=session, original_filename="kept.pdf")
        self.kept.file.save("kept.pdf", ContentFile(b"k"), save=True)
        self.orphan = default_storage.save("gtm/delivery/2026/01/01/orphan.pdf", ContentFile(b"o"))
        self.outside = default_storage.save("exports/not_ours.pdf", ContentFile(b"x"))
        self.stale = Workspace.objects.create(name="Old Co", is_active=False)
        self.live = Workspace.objects.create(name="Live Co")

    def _run(self, *args):
        from django.core.management import call_command
        with self.captureOnCommitCallbacks(execute=True):
            call_command("purge_stale_data", *args, stdout=self.out)

    def test_dry_run_changes_nothing(self):
        self._run()
        self.assertTrue(default_storage.exists(self.orphan))
        self.assertTrue(Workspace.objects.filter(pk=self.stale.pk).exists())
        self.assertIn("Would delete 1 orphaned file(s).", self.out.getvalue())

    def test_apply_deletes_only_orphans_and_deactivated_workspaces(self):
        self._run("--apply")
        self.assertFalse(default_storage.exists(self.orphan))
        self.assertTrue(default_storage.exists(self.kept.file.name))
        self.assertTrue(default_storage.exists(self.outside))
        self.assertFalse(Workspace.objects.filter(pk=self.stale.pk).exists())
        self.assertTrue(Workspace.objects.filter(pk=self.live.pk).exists())
