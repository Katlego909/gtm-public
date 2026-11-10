"""
Signup funnel and feature usage for the staff-only funnel page.

Everything is computed from rows the app already writes; the only table that
exists purely for this is UserActiveDay (the "came back" signal -- see its
docstring). One query per source, independent of cohort size.

The cohort is the users who signed up inside the chosen window. Each step
counts how many of them have *ever* done the thing, so a user who signed up
in the window and acted later still counts.
"""
from dataclasses import dataclass
from datetime import timedelta

from django.contrib.auth import get_user_model

from dashboard.models import GapMetricMeasurement, Resource
from .models import (
    ActionItemComment, AssessmentSession, CategoryDocument, ChatMessage,
    DeliveryDocument, GTMFile, WorkspaceChatMessage,
)
from .models_agent_runs import AgentRun
from .models_automations import Automation
from .models_beta import UserActiveDay
from .models_integrations import WorkspaceApiKey, WorkspaceIntegration
from .models_workspace import WorkspaceActivityEvent

User = get_user_model()

RETURN_AFTER_DAYS = 7


@dataclass
class Step:
    label: str
    count: int
    base: int  # denominator for the conversion rate shown next to the step
    note: str = ""

    @property
    def pct_of_base(self):
        return round(100 * self.count / self.base) if self.base else 0


def _ids(qs, field):
    return set(qs.values_list(field, flat=True).distinct())


def build_funnel(start, end, now):
    """Return (steps, feature_rows, cohort_size) for users who joined in [start, end)."""
    cohort = User.objects.filter(date_joined__gte=start, date_joined__lt=end)
    cohort_ids = set(cohort.values_list("id", flat=True))
    n = len(cohort_ids)

    started = _ids(AssessmentSession.objects.filter(user_id__in=cohort_ids), "user_id")
    completed = _ids(AssessmentSession.objects.filter(user_id__in=cohort_ids, is_completed=True), "user_id")

    # "Acted on results": a human step taken on the output, not just reading it.
    acted = (
        _ids(WorkspaceActivityEvent.objects.filter(
            actor_id__in=cohort_ids, event_type__in=["task_created", "task_moved"]), "actor_id")
        | _ids(ActionItemComment.objects.filter(user_id__in=cohort_ids), "user_id")
        | _ids(AgentRun.objects.filter(approved_by_id__in=cohort_ids), "approved_by_id")
        | _ids(GapMetricMeasurement.objects.filter(recorded_by_id__in=cohort_ids), "recorded_by_id")
    )

    # Only users who joined at least RETURN_AFTER_DAYS ago can have come back yet.
    # Compared in Python: date-vs-datetime arithmetic differs between SQLite
    # (string compare) and Postgres, and the row count is small.
    joined = dict(cohort.filter(
        date_joined__lte=now - timedelta(days=RETURN_AFTER_DAYS)).values_list("id", "date_joined"))
    eligible_ids = set(joined)
    returned = {
        user_id for user_id, day in UserActiveDay.objects.filter(
            user_id__in=eligible_ids).values_list("user_id", "day")
        if day >= joined[user_id].date() + timedelta(days=RETURN_AFTER_DAYS)
    }

    steps = [
        Step("Signed up", n, n),
        Step("Started an assessment", len(started), n),
        Step("Completed an assessment", len(completed), n),
        Step("Acted on results", len(acted), n,
             "Created or moved a task, commented, approved an agent run, or logged a measured value."),
        Step(f"Came back {RETURN_AFTER_DAYS}+ days after signup", len(returned), len(eligible_ids),
             f"Out of the {len(eligible_ids)} who signed up at least {RETURN_AFTER_DAYS} days ago. "
             "Tracked from when active-day recording shipped; earlier visits are not recorded."),
    ]

    in_cohort = {"user_id__in": cohort_ids}
    features = [
        ("Assessment chat", _ids(ChatMessage.objects.filter(**in_cohort), "user_id")),
        ("Agent chat", _ids(WorkspaceChatMessage.objects.filter(**in_cohort), "user_id")),
        ("Requested an agent run", _ids(AgentRun.objects.filter(requested_by_id__in=cohort_ids), "requested_by_id")),
        ("Approved an agent run", _ids(AgentRun.objects.filter(approved_by_id__in=cohort_ids), "approved_by_id")),
        ("Uploaded evidence", (
            _ids(GTMFile.objects.filter(session__user_id__in=cohort_ids), "session__user_id")
            | _ids(DeliveryDocument.objects.filter(session__user_id__in=cohort_ids), "session__user_id")
            | _ids(CategoryDocument.objects.filter(session__user_id__in=cohort_ids), "session__user_id")
            | _ids(Resource.objects.filter(uploaded_by_id__in=cohort_ids, file__gt=""), "uploaded_by_id")
        )),
        ("Commented on a task", _ids(ActionItemComment.objects.filter(**in_cohort), "user_id")),
        ("Logged a measured gap value", _ids(
            GapMetricMeasurement.objects.filter(recorded_by_id__in=cohort_ids), "recorded_by_id")),
        ("Created an automation", _ids(Automation.objects.filter(created_by_id__in=cohort_ids), "created_by_id")),
        ("Connected an integration", _ids(
            WorkspaceIntegration.objects.filter(configured_by_id__in=cohort_ids), "configured_by_id")),
        ("Created an API key", _ids(WorkspaceApiKey.objects.filter(created_by_id__in=cohort_ids), "created_by_id")),
    ]
    feature_rows = sorted(
        ({"label": label, "count": len(ids - {None}), "pct": round(100 * len(ids - {None}) / n) if n else 0}
         for label, ids in features),
        key=lambda r: -r["count"],
    )
    return steps, feature_rows, n
