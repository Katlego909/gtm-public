"""
Tests for the app-wide notification system: preference-gated delivery
(dashboard/utils_notifications.py), the mark-read/mark-all-read endpoints,
and the trigger points that create Notifications (task completion, AI task
completion, workspace invites).
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from gtm.models import ActionItem, AssessmentSession
from gtm.models_workspace import Workspace, WorkspaceMembership, WorkspaceInvitation
from dashboard.models import Notification, UserSettings
from dashboard.utils_notifications import send_notification

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class SendNotificationPreferenceTests(TestCase):
    """send_notification() must respect each recipient's UserSettings
    in-app/email toggles independently per notification_type."""

    @classmethod
    def setUpTestData(cls):
        cls.recipient = User.objects.create_user(
            username="recipient", password="pass1234", email="recipient@test.com"
        )

    def test_creates_inapp_row_by_default(self):
        # UserSettings defaults both inapp_task_assigned and email_task_assigned
        # to True, so a first-time notification creates the row AND emails.
        send_notification(self.recipient, "Hi", "A message", notification_type='task')
        self.assertEqual(Notification.objects.filter(recipient=self.recipient).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_inapp_toggle_off_suppresses_notification_row(self):
        settings_row, _ = UserSettings.objects.get_or_create(user=self.recipient)
        settings_row.inapp_task_assigned = False
        settings_row.save()

        result = send_notification(self.recipient, "Hi", "A message", notification_type='task')
        self.assertIsNone(result)
        self.assertEqual(Notification.objects.filter(recipient=self.recipient).count(), 0)

    def test_email_toggle_on_sends_email(self):
        settings_row, _ = UserSettings.objects.get_or_create(user=self.recipient)
        settings_row.email_task_assigned = True
        settings_row.save()

        send_notification(self.recipient, "Hi", "A message", notification_type='task', link="/dashboard/")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.recipient.email])

    def test_email_toggle_off_suppresses_email(self):
        settings_row, _ = UserSettings.objects.get_or_create(user=self.recipient)
        settings_row.email_task_assigned = False
        settings_row.save()

        send_notification(self.recipient, "Hi", "A message", notification_type='task')
        self.assertEqual(len(mail.outbox), 0)

    def test_system_type_always_inapp_and_emails_by_default(self):
        settings_row, _ = UserSettings.objects.get_or_create(user=self.recipient)
        # 'system' has no in-app gate (row always created) and now emails by
        # default, gated by email_system -- part of email-for-everything.
        send_notification(self.recipient, "Welcome", "You're in", notification_type='system')
        self.assertEqual(Notification.objects.filter(recipient=self.recipient).count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_system_email_can_be_opted_out(self):
        settings_row, _ = UserSettings.objects.get_or_create(user=self.recipient)
        settings_row.email_system = False
        settings_row.save()
        send_notification(self.recipient, "Welcome", "You're in", notification_type='system')
        # Still shown in-app, but no email once opted out.
        self.assertEqual(Notification.objects.filter(recipient=self.recipient).count(), 1)
        self.assertEqual(len(mail.outbox), 0)

    def test_unauthenticated_recipient_is_noop(self):
        from django.contrib.auth.models import AnonymousUser

        result = send_notification(AnonymousUser(), "Hi", "msg")
        self.assertIsNone(result)


@override_settings(SECURE_SSL_REDIRECT=False)
class NotificationEndpointTests(TestCase):
    """notification_dropdown / notification_mark_read / notifications_mark_all_read."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="bell-user", password="pass1234", email="bell-user@test.com"
        )
        cls.other_user = User.objects.create_user(
            username="other-user", password="pass1234", email="other-user@test.com"
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_dropdown_lists_only_own_notifications(self):
        Notification.objects.create(recipient=self.user, title="Mine", message="m")
        Notification.objects.create(recipient=self.other_user, title="Not mine", message="m")

        response = self.client.get(reverse("notification_dropdown"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mine")
        self.assertNotContains(response, "Not mine")

    def test_unread_count_endpoint_renders_badge(self):
        Notification.objects.create(recipient=self.user, title="Unread", message="m", is_read=False)
        response = self.client.get(reverse("notification_unread_count"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1")

    def test_mark_read_only_affects_own_notification(self):
        mine = Notification.objects.create(recipient=self.user, title="Mine", message="m")
        others = Notification.objects.create(recipient=self.other_user, title="Not mine", message="m")

        response = self.client.post(reverse("notification_mark_read", args=[others.id]))
        self.assertEqual(response.status_code, 404)
        others.refresh_from_db()
        self.assertFalse(others.is_read)

        response = self.client.post(reverse("notification_mark_read", args=[mine.id]))
        self.assertEqual(response.status_code, 200)
        mine.refresh_from_db()
        self.assertTrue(mine.is_read)

    def test_mark_all_read(self):
        Notification.objects.create(recipient=self.user, title="A", message="m")
        Notification.objects.create(recipient=self.user, title="B", message="m")
        Notification.objects.create(recipient=self.other_user, title="Not mine", message="m")

        response = self.client.post(reverse("notifications_mark_all_read"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Notification.objects.filter(recipient=self.user, is_read=False).count(), 0)
        self.assertEqual(Notification.objects.filter(recipient=self.other_user, is_read=False).count(), 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class TaskCompletionNotificationTests(TestCase):
    """move_action_item (kanban drag) and add_edit_action_item (form edit)
    must notify the task's creator when someone else completes it."""

    @classmethod
    def setUpTestData(cls):
        cls.creator = User.objects.create_user(
            username="creator", password="pass1234", email="creator@test.com"
        )
        cls.completer = User.objects.create_user(
            username="completer", password="pass1234", email="completer@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Task Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.creator, role="admin", is_active=True
        )
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.completer, role="admin", is_active=True
        )

    def setUp(self):
        self.client.force_login(self.completer)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

    def _make_item(self, **kwargs):
        defaults = dict(
            workspace=self.workspace,
            note="Ship the onboarding email",
            status="todo",
            created_by=self.creator,
        )
        defaults.update(kwargs)
        return ActionItem.objects.create(**defaults)

    def test_move_to_done_notifies_creator(self):
        item = self._make_item()
        response = self.client.post(reverse("move_action_item", args=[item.id, "done"]))
        self.assertEqual(response.status_code, 200)

        notification = Notification.objects.get(recipient=self.creator, notification_type='task_status')
        self.assertEqual(notification.sender, self.completer)
        self.assertIn("Task Completed", notification.title)

    def test_move_to_done_by_creator_does_not_self_notify(self):
        self.client.force_login(self.creator)
        session = self.client.session
        session["current_workspace_id"] = str(self.workspace.id)
        session.save()

        item = self._make_item()
        self.client.post(reverse("move_action_item", args=[item.id, "done"]))
        self.assertFalse(Notification.objects.filter(recipient=self.creator, notification_type='task_status').exists())

    def test_moving_between_non_done_statuses_does_not_notify(self):
        item = self._make_item()
        self.client.post(reverse("move_action_item", args=[item.id, "doing"]))
        self.assertFalse(Notification.objects.filter(notification_type='task_status').exists())

    def test_edit_form_transition_to_done_notifies_creator(self):
        # add_edit_action_item only allows the creator or assignee to edit,
        # so the assignee (not the creator) is the one completing it here.
        item = self._make_item(assigned_to=self.completer)
        response = self.client.post(
            reverse("edit_action_item", args=[item.id]),
            {"note": item.note, "status": "done", "due_date": ""},
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        notification = Notification.objects.get(recipient=self.creator, notification_type='task_status')
        self.assertEqual(notification.sender, self.completer)


@override_settings(SECURE_SSL_REDIRECT=False)
class AICompletionNotificationTests(TestCase):
    """dashboard/views/actions.py::_complete_action_item_background must
    notify the creator/assignee once the AI turn actually finishes an item."""

    @classmethod
    def setUpTestData(cls):
        cls.creator = User.objects.create_user(
            username="ai-creator", password="pass1234", email="ai-creator@test.com"
        )
        cls.triggerer = User.objects.create_user(
            username="ai-triggerer", password="pass1234", email="ai-triggerer@test.com"
        )
        cls.workspace = Workspace.objects.create(name="AI Notify Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.creator, role="admin", is_active=True
        )
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.triggerer, role="admin", is_active=True
        )
        cls.session = AssessmentSession.objects.create(
            owner_client_id="ai-notify-client",
            user=cls.triggerer,
            workspace=cls.workspace,
            company_name="AI Notify Co",
        )

    def _make_item(self, **kwargs):
        defaults = dict(
            session=self.session,
            workspace=self.workspace,
            note="Define ICP inclusion/exclusion criteria.",
            status="todo",
            created_by=self.creator,
        )
        defaults.update(kwargs)
        return ActionItem.objects.create(**defaults)

    def test_successful_ai_completion_notifies_creator(self):
        from dashboard.views.actions import _complete_action_item_background

        item = self._make_item()
        with mock.patch("gtm.action_item_completion.complete_action_item") as mock_complete:
            mock_complete.return_value = {"success": True, "status": "done", "summary": "Done."}
            _complete_action_item_background(item.id, self.triggerer.id, self.workspace.id)

        notification = Notification.objects.get(recipient=self.creator, notification_type='task_status')
        self.assertIn("AI", notification.title)

    def test_partial_ai_completion_does_not_notify(self):
        from dashboard.views.actions import _complete_action_item_background

        item = self._make_item()
        with mock.patch("gtm.action_item_completion.complete_action_item") as mock_complete:
            mock_complete.return_value = {"success": True, "status": "doing", "summary": "Partial."}
            _complete_action_item_background(item.id, self.triggerer.id, self.workspace.id)

        self.assertFalse(Notification.objects.filter(notification_type='task_status').exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceInviteNotificationTests(TestCase):
    """gtm/views_workspace.py::workspace_invite must surface an in-app
    Notification for invitees who already have an account."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="invite-admin", password="pass1234", email="invite-admin@test.com"
        )
        cls.existing_user = User.objects.create_user(
            username="existing", password="pass1234", email="existing@test.com"
        )
        cls.workspace = Workspace.objects.create(name="Invite Co")
        WorkspaceMembership.objects.create(
            workspace=cls.workspace, user=cls.admin, role="admin", is_active=True
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_inviting_existing_user_creates_notification(self):
        url = reverse("gtm:workspace:invite", args=[self.workspace.id])
        response = self.client.post(url, {"email": "existing@test.com", "role": "contributor"})
        self.assertEqual(response.status_code, 302)

        notification = Notification.objects.get(recipient=self.existing_user, notification_type='invite')
        self.assertEqual(notification.sender, self.admin)

    def test_inviting_unregistered_email_creates_no_notification(self):
        url = reverse("gtm:workspace:invite", args=[self.workspace.id])
        response = self.client.post(url, {"email": "nobody@test.com", "role": "contributor"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Notification.objects.filter(notification_type='invite').exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class AutoJoinWelcomeNotificationTests(TestCase):
    """gtm/signals.py::auto_join_invited_workspaces must welcome a user who
    registers with an email that had a pending workspace invitation."""

    def test_registration_with_pending_invite_sends_welcome_notification(self):
        from django.utils import timezone
        import datetime

        inviter = User.objects.create_user(
            username="welcome-inviter", password="pass1234", email="inviter@test.com"
        )
        workspace = Workspace.objects.create(name="Welcome Co")
        WorkspaceInvitation.objects.create(
            workspace=workspace,
            email="newbie@test.com",
            role="contributor",
            invited_by=inviter,
            expires_at=timezone.now() + datetime.timedelta(days=7),
        )

        new_user = User.objects.create_user(
            username="newbie", password="pass1234", email="newbie@test.com"
        )

        notification = Notification.objects.get(recipient=new_user, notification_type='system')
        self.assertIn("Welcome", notification.title)
        self.assertTrue(
            WorkspaceMembership.objects.filter(workspace=workspace, user=new_user).exists()
        )
