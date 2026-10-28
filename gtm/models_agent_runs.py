import uuid

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


class AgentRun(models.Model):
    """One piece of agent work with a human in the loop: the agent
    investigates and proposes a plan, a person approves it, and only then
    does the agent act -- each write tool gated against the approved steps
    (see gtm/agent_runs.py). `plan` and `log` are the audit trail."""

    STATUS_CHOICES = [
        ("planning", "Planning"),
        ("awaiting_approval", "Awaiting approval"),
        ("running", "Running"),
        ("waiting_for_input", "Waiting for input"),
        ("completed", "Completed"),
        ("failed", "Failed"),
        ("rejected", "Rejected"),
        ("cancelled", "Cancelled"),
    ]
    # Statuses a background worker may currently be driving.
    ACTIVE_STATUSES = ("planning", "running")
    # Statuses that need a person to do something next.
    NEEDS_HUMAN_STATUSES = ("awaiting_approval", "waiting_for_input")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        "Workspace", on_delete=models.CASCADE, null=True, blank=True, related_name="agent_runs",
    )
    session = models.ForeignKey(
        "AssessmentSession", on_delete=models.CASCADE, null=True, blank=True, related_name="agent_runs",
    )
    action_item = models.ForeignKey(
        "ActionItem", on_delete=models.CASCADE, null=True, blank=True, related_name="agent_runs",
    )
    agent_type = models.CharField(max_length=20)
    goal = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="planning", db_index=True)

    # [{id, kind, title, detail, status: pending|done|skipped|failed, result, refs}]
    plan = models.JSONField(default=list, blank=True, encoder=DjangoJSONEncoder)
    plan_summary = models.TextField(blank=True, default="")
    # [{at, event, ...}] -- tool calls, results, approvals, answers.
    log = models.JSONField(default=list, blank=True, encoder=DjangoJSONEncoder)
    feedback = models.TextField(blank=True, default="", help_text="Reviewer feedback used when re-planning.")
    result_summary = models.TextField(blank=True, default="")
    error = models.CharField(max_length=240, blank=True, default="")

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="requested_agent_runs",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_agent_runs",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    total_tokens = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["workspace", "status"])]

    def __str__(self):
        return f"{self.agent_type} run {self.id} ({self.status})"

    @property
    def agent_name(self):
        from .agent_runtime import agent_name

        return agent_name(self.agent_type, default=self.agent_type)

    @property
    def active_steps(self):
        return [s for s in self.plan if s.get("status") != "skipped"]

    @property
    def done_step_count(self):
        return sum(1 for s in self.plan if s.get("status") == "done")


class AgentRunQuestion(models.Model):
    """A question an agent asked a teammate mid-run; the run waits in
    `waiting_for_input` until it's answered, then resumes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(AgentRun, on_delete=models.CASCADE, related_name="questions")
    asked_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_questions",
    )
    question = models.TextField()
    answer = models.TextField(blank=True, default="")
    answered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="answered_agent_questions",
    )
    answered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    @property
    def is_open(self):
        return self.answered_at is None
