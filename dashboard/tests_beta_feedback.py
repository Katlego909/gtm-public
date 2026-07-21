"""
Regression tests for beta feedback capture: dashboard/views/analytics.py's
insight_feedback (previously a print()-and-discard stub) and the new
general_feedback endpoint, both persisting to dashboard/models.py::BetaFeedback.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse

from dashboard.models import BetaFeedback

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class InsightFeedbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fbuser", password="pass1234", email="fb@test.com")
        self.client.force_login(self.user)

    def test_feedback_is_persisted_not_discarded(self):
        response = self.client.post(reverse("insight_feedback", args=[42]), {"feedback": "This insight was confusing."})
        self.assertEqual(response.status_code, 200)

        row = BetaFeedback.objects.get()
        self.assertEqual(row.context, "insight")
        self.assertEqual(row.reference_id, "42")
        self.assertEqual(row.message, "This insight was confusing.")
        self.assertEqual(row.user, self.user)

    def test_blank_feedback_is_not_persisted(self):
        self.client.post(reverse("insight_feedback", args=[42]), {"feedback": "   "})
        self.assertFalse(BetaFeedback.objects.exists())

    def test_anonymous_feedback_has_no_user(self):
        self.client.logout()
        self.client.post(reverse("insight_feedback", args=[7]), {"feedback": "Anonymous note"})
        row = BetaFeedback.objects.get()
        self.assertIsNone(row.user)


@override_settings(SECURE_SSL_REDIRECT=False)
class GeneralFeedbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="genfbuser", password="pass1234", email="genfb@test.com")
        self.client.force_login(self.user)

    def test_general_feedback_is_persisted_with_general_context(self):
        response = self.client.post(
            reverse("general_feedback"),
            {"feedback": "The assessment flow confused me at step 3.", "page_url": "https://example.com/assess/1/"},
        )
        self.assertEqual(response.status_code, 200)

        row = BetaFeedback.objects.get()
        self.assertEqual(row.context, "general")
        self.assertEqual(row.reference_id, "")
        self.assertEqual(row.page_url, "https://example.com/assess/1/")
        self.assertEqual(row.user, self.user)

    def test_visible_in_admin_list(self):
        BetaFeedback.objects.create(user=self.user, context="general", message="Some feedback")
        admin_user = User.objects.create_superuser(username="admin", password="pass1234", email="admin@test.com")
        self.client.force_login(admin_user)
        response = self.client.get(reverse("admin:dashboard_betafeedback_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Some feedback")
