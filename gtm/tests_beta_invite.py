from datetime import timedelta

from django.contrib.auth import get_user_model
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
