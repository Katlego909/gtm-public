import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class Automation(models.Model):
    """A rule that keeps running after the conversation ends: on a schedule,
    or whenever a gap metric is off target or tasks go overdue, it creates a
    task or notifies people. Rules are deterministic (gtm/automations.py) --
    no AI call or credits per run. Agents can only create one through an
    approved plan step; people can pause or delete them any time."""

    TRIGGER_CHOICES = [
        ("schedule", "On a schedule"),
        ("metric_off_target", "While a gap metric is off target"),
        ("tasks_overdue", "When tasks are overdue"),
    ]
    ACTION_CHOICES = [
        ("create_task", "Create a task"),
        ("notify", "Notify people"),
    ]
    FREQUENCY_CHOICES = [
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("monthly", "Monthly"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey("Workspace", on_delete=models.CASCADE, related_name="automations")
    session = models.ForeignKey(
        "AssessmentSession", on_delete=models.SET_NULL, null=True, blank=True, related_name="automations",
        help_text="Assessment that tasks this automation creates are linked to, if any.",
    )
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True, default="")
    trigger = models.CharField(max_length=20, choices=TRIGGER_CHOICES)
    frequency = models.CharField(max_length=10, choices=FREQUENCY_CHOICES, default="weekly")
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    # create_task: {note, assignee, due_in_days}; notify: {recipients: [...], message};
    # metric_off_target: {metric}
    config = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    is_active = models.BooleanField(default=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_by_run = models.ForeignKey(
        "AgentRun", on_delete=models.SET_NULL, null=True, blank=True, related_name="automations",
    )
    agent_type = models.CharField(max_length=20, blank=True, default="")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    next_run_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.get_trigger_display()}, {self.frequency})"


class AutomationRun(models.Model):
    """One evaluation of an automation: what it did, or why it didn't."""

    OUTCOME_CHOICES = [
        ("acted", "Acted"),
        ("skipped", "Nothing to do"),
        ("failed", "Failed"),
    ]

    automation = models.ForeignKey(Automation, on_delete=models.CASCADE, related_name="runs")
    ran_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(max_length=10, choices=OUTCOME_CHOICES)
    detail = models.CharField(max_length=500, blank=True, default="")
    refs = models.JSONField(default=list, blank=True, encoder=DjangoJSONEncoder)

    class Meta:
        ordering = ["-ran_at"]
