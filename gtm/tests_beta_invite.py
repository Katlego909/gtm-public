from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from gtm.models_beta import BetaInviteCode

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class BetaInviteSignupTests(TestCase):
    def _signup_payload(self, invite_code="", email="new@example.com", username="newtester"):
        return {
            "username": username,
            "email": email,
            "password1": "a-strong-passw0rd!",
            "password2": "a-strong-passw0rd!",
            "invite_code": invite_code,
        }

    def test_valid_code_allows_signup_and_marks_code_used(self):
        invite = BetaInviteCode.objects.create(note="Test tester")
        response = self.client.post(reverse("account_signup"), self._signup_payload(invite_code=invite.code))

        self.assertTrue(User.objects.filter(username="newtester").exists())
        invite.refresh_from_db()
        self.assertTrue(invite.is_used)
        self.assertEqual(invite.used_by.username, "newtester")
        self.assertIsNotNone(invite.used_at)

    def test_missing_code_is_rejected(self):
        response = self.client.post(reverse("account_signup"), self._signup_payload(invite_code=""))
        self.assertFalse(User.objects.filter(username="newtester").exists())
        self.assertContains(response, "field is required", status_code=200)

    def test_unknown_code_is_rejected(self):
        response = self.client.post(reverse("account_signup"), self._signup_payload(invite_code="NOTREAL1"))
        self.assertFalse(User.objects.filter(username="newtester").exists())
        self.assertContains(response, "invite-only", status_code=200)

    def test_already_used_code_is_rejected(self):
        other_user = User.objects.create_user(username="already", email="already@example.com", password="x")
        invite = BetaInviteCode.objects.create(used_by=other_user, used_at=timezone.now())

        response = self.client.post(reverse("account_signup"), self._signup_payload(invite_code=invite.code))
        self.assertFalse(User.objects.filter(username="newtester").exists())
        self.assertContains(response, "already been used", status_code=200)

    def test_expired_code_is_rejected(self):
        invite = BetaInviteCode.objects.create(expires_at=timezone.now() - timedelta(days=1))
        response = self.client.post(reverse("account_signup"), self._signup_payload(invite_code=invite.code))
        self.assertFalse(User.objects.filter(username="newtester").exists())
        self.assertContains(response, "expired", status_code=200)

    def test_code_is_case_and_whitespace_insensitive(self):
        invite = BetaInviteCode.objects.create()
        response = self.client.post(
            reverse("account_signup"),
            self._signup_payload(invite_code=f"  {invite.code.lower()}  "),
        )
        self.assertTrue(User.objects.filter(username="newtester").exists())

    def test_signup_page_prefills_code_from_query_param(self):
        response = self.client.get(reverse("account_signup") + "?code=ABC12345")
        self.assertContains(response, 'value="ABC12345"')


@override_settings(SECURE_SSL_REDIRECT=False)
class BetaInviteAdminEmailTests(TestCase):
    """Saving a code with an email in the admin should mail the invite, once."""

    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username="operator", email="operator@example.com", password="admin-passw0rd!"
        )
        self.client.force_login(self.admin_user)
        self.add_url = reverse("admin:gtm_betainvitecode_add")
        self.changelist_url = reverse("admin:gtm_betainvitecode_changelist")

    def _form_payload(self, **overrides):
        payload = {"code": "TESTCODE", "email": "", "note": "", "expires_at_0": "", "expires_at_1": ""}
        payload.update(overrides)
        return payload

    def _change_url(self, invite):
        return reverse("admin:gtm_betainvitecode_change", args=[invite.pk])

    def test_saving_with_email_sends_invite_and_stamps_sent_at(self):
        response = self.client.post(
            self.add_url, self._form_payload(email="jane@acme.com", note="Jane @ Acme")
        )
        self.assertRedirects(response, self.changelist_url)

        invite = BetaInviteCode.objects.get(code="TESTCODE")
        self.assertIsNotNone(invite.sent_at)
        self.assertTrue(invite.is_sent)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["jane@acme.com"])

    def test_sent_email_contains_code_and_prefilled_signup_link(self):
        self.client.post(self.add_url, self._form_payload(email="jane@acme.com"))

        message = mail.outbox[0]
        expected_link = f"{reverse('account_signup')}?code=TESTCODE"
        self.assertIn("TESTCODE", message.body)
        self.assertIn(expected_link, message.body)
        html_body = message.alternatives[0][0]
        self.assertIn(expected_link, html_body)

    def test_saving_without_email_sends_nothing(self):
        self.client.post(self.add_url, self._form_payload(note="Hand out at the meetup"))

        invite = BetaInviteCode.objects.get(code="TESTCODE")
        self.assertIsNone(invite.sent_at)
        self.assertEqual(len(mail.outbox), 0)

    def test_unrelated_edit_does_not_resend(self):
        self.client.post(self.add_url, self._form_payload(email="jane@acme.com"))
        invite = BetaInviteCode.objects.get(code="TESTCODE")
        first_sent_at = invite.sent_at

        self.client.post(
            self._change_url(invite),
            self._form_payload(email="jane@acme.com", note="Followed up on Tuesday"),
        )

        invite.refresh_from_db()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(invite.sent_at, first_sent_at)
        self.assertEqual(invite.note, "Followed up on Tuesday")

    def test_changing_the_address_resends_to_the_new_one(self):
        self.client.post(self.add_url, self._form_payload(email="typo@acme.com"))
        invite = BetaInviteCode.objects.get(code="TESTCODE")

        self.client.post(self._change_url(invite), self._form_payload(email="jane@acme.com"))

        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(mail.outbox[1].to, ["jane@acme.com"])

    def test_adding_an_address_to_a_blank_code_sends(self):
        """The "Generate N codes" toolbar creates codes with no address."""
        invite = BetaInviteCode.objects.create(note="Generated from admin")

        self.client.post(
            self._change_url(invite),
            self._form_payload(code=invite.code, email="jane@acme.com", note="Generated from admin"),
        )

        invite.refresh_from_db()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIsNotNone(invite.sent_at)

    def test_smtp_failure_still_saves_the_code_and_leaves_it_unsent(self):
        with patch(
            "gtm.utils_email.EmailMultiAlternatives.send", side_effect=Exception("SMTP down")
        ):
            response = self.client.post(
                self.add_url, self._form_payload(email="jane@acme.com"), follow=True
            )

        self.assertEqual(response.status_code, 200)
        invite = BetaInviteCode.objects.get(code="TESTCODE")
        self.assertIsNone(invite.sent_at)
        self.assertContains(response, "SMTP down")

    def test_resend_action_forces_a_second_send(self):
        self.client.post(self.add_url, self._form_payload(email="jane@acme.com"))
        invite = BetaInviteCode.objects.get(code="TESTCODE")
        first_sent_at = invite.sent_at

        self.client.post(
            self.changelist_url,
            {"action": "email_invite_code", "_selected_action": [str(invite.pk)]},
        )

        invite.refresh_from_db()
        self.assertEqual(len(mail.outbox), 2)
        self.assertGreater(invite.sent_at, first_sent_at)

    def test_resend_action_skips_codes_with_no_address(self):
        invite = BetaInviteCode.objects.create(note="No address on file")

        self.client.post(
            self.changelist_url,
            {"action": "email_invite_code", "_selected_action": [str(invite.pk)]},
        )

        invite.refresh_from_db()
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(invite.sent_at)

    def test_created_by_is_recorded_on_add(self):
        self.client.post(self.add_url, self._form_payload(email="jane@acme.com"))

        invite = BetaInviteCode.objects.get(code="TESTCODE")
        self.assertEqual(invite.created_by, self.admin_user)

    @override_settings(EMAIL_REDIRECT_TO="staging-catchall@example.com")
    def test_email_redirect_to_overrides_the_recipient(self):
        self.client.post(self.add_url, self._form_payload(email="jane@acme.com"))

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["staging-catchall@example.com"])
