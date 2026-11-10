from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.utils import override_settings
from django.urls import reverse
from django.utils import timezone

from gtm.funnel import build_funnel
from gtm.models import ActionItemComment, ActionItem, AssessmentSession
from gtm.models_beta import UserActiveDay

User = get_user_model()


def _joined(username, days_ago):
    user = User.objects.create_user(username=username, password="pass1234")
    User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(days=days_ago))
    user.refresh_from_db()
    return user


class FunnelCountsTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.old = _joined("old", 20)        # completes, acts, returns on day 10
        self.mid = _joined("mid", 15)        # starts only, active only on signup day
        self.new = _joined("new", 2)         # too recent to count for "came back"
        _joined("outside", 200)              # outside a 90-day window

        AssessmentSession.objects.create(owner_client_id="a", user=self.old, is_completed=True)
        AssessmentSession.objects.create(owner_client_id="b", user=self.mid)
        item = ActionItem.objects.create(note="t")
        ActionItemComment.objects.create(action_item=item, user=self.old, text="done")

        UserActiveDay.objects.create(user=self.old, day=(self.old.date_joined + timedelta(days=10)).date())
        UserActiveDay.objects.create(user=self.mid, day=self.mid.date_joined.date())
        UserActiveDay.objects.create(user=self.new, day=self.now.date())

    def _steps(self):
        steps, features, n = build_funnel(self.now - timedelta(days=90), self.now, self.now)
        return {s.label.split(" ")[0] + " " + s.label.split(" ")[1]: s for s in steps}, features, n

    def test_cohort_excludes_users_outside_window(self):
        _, _, n = self._steps()
        self.assertEqual(n, 3)

    def test_step_counts(self):
        steps, _, _ = self._steps()
        self.assertEqual(steps["Started an"].count, 2)
        self.assertEqual(steps["Completed an"].count, 1)
        self.assertEqual(steps["Acted on"].count, 1)

    def test_came_back_only_counts_activity_after_day_seven_among_eligible(self):
        steps, _, _ = self._steps()
        came_back = steps["Came back"]
        self.assertEqual(came_back.count, 1)   # old only; mid was active on signup day
        self.assertEqual(came_back.base, 2)    # new joined 2 days ago, not eligible

    def test_feature_usage_counts_commenter(self):
        _, features, _ = self._steps()
        row = next(f for f in features if f["label"] == "Commented on a task")
        self.assertEqual(row["count"], 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class FunnelViewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(username="staff", password="pass1234", is_staff=True)
        self.regular = User.objects.create_user(username="regular", password="pass1234")

    def test_staff_sees_page(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("gtm:beta_funnel"), {"days": "30"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Completed an assessment")

    def test_bad_window_falls_back_to_default(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("gtm:beta_funnel"), {"days": "abc"})
        self.assertEqual(response.context["window"], "90")

    def test_non_staff_denied(self):
        self.client.force_login(self.regular)
        self.assertNotEqual(self.client.get(reverse("gtm:beta_funnel")).status_code, 200)


@override_settings(SECURE_SSL_REDIRECT=False)
class RecordActiveDayMiddlewareTests(TestCase):
    def test_signed_in_requests_record_one_row_per_day(self):
        user = User.objects.create_user(username="visitor", password="pass1234")
        self.client.force_login(user)
        self.client.get("/")
        self.client.get("/")
        self.assertEqual(UserActiveDay.objects.filter(user=user, day=timezone.now().date()).count(), 1)

    def test_anonymous_requests_record_nothing(self):
        self.client.get("/")
        self.assertFalse(UserActiveDay.objects.exists())
