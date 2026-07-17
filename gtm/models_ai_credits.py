# gtm/models_ai_credits.py
"""
Per-workspace/per-user Gemini token budget ledger. AICreditAccount is the
spend-control layer that gtm/ai_credits.py's resolve/check/record functions
read and write -- distinct from gtm/utils_ai_monitoring.py's AIUsageTracker,
which is a global, cache-backed, purely observational counter against
Google's own free-tier quota ceiling and is unrelated to this per-owner
enforcement.

DB-backed (not cache-backed) because CACHES isn't configured in settings.py
(Django defaults to per-process LocMemCache) and the app deploys to Cloud
Run, which can run multiple instances/processes -- a cache counter would
silently under-count spend across instances. Resets are lazy (computed at
read/write time) because there's no Celery/cron in this app to run a
scheduled reset job.
"""
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .models_workspace import Workspace


class AICreditAccount(models.Model):
    """A Gemini token budget ledger. Exactly one of workspace/user is set --
    workspace pools are the primary unit (Workspace is already this app's
    collaboration/billing unit), the personal pool is the fallback for an
    authenticated user who isn't in a workspace yet."""

    PERIOD_DAILY = "daily"
    PERIOD_WEEKLY = "weekly"
    PERIOD_CHOICES = [(PERIOD_DAILY, "Daily"), (PERIOD_WEEKLY, "Weekly")]

    DEFAULT_WORKSPACE_TOKEN_BUDGET = 500_000
    DEFAULT_PERSONAL_TOKEN_BUDGET = 100_000

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        Workspace, on_delete=models.CASCADE, null=True, blank=True,
        related_name="ai_credit_account",
    )
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
        related_name="ai_credit_account",
    )

    period_length = models.CharField(max_length=10, choices=PERIOD_CHOICES, default=PERIOD_DAILY)
    token_budget = models.PositiveIntegerField(
        default=DEFAULT_WORKSPACE_TOKEN_BUDGET,
        help_text="Total Gemini prompt+output tokens allowed per period.",
    )
    tokens_used = models.PositiveIntegerField(default=0)
    period_started_at = models.DateTimeField(default=timezone.now)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                check=(
                    Q(workspace__isnull=False, user__isnull=True)
                    | Q(workspace__isnull=True, user__isnull=False)
                ),
                name="ai_credit_account_exactly_one_owner",
            ),
        ]
        indexes = [
            models.Index(fields=["workspace"]),
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        owner = self.workspace.name if self.workspace_id else (self.user.get_username() if self.user_id else "?")
        return f"AI credits: {owner}"

    def _period_timedelta(self):
        return timedelta(days=1) if self.period_length == self.PERIOD_DAILY else timedelta(days=7)

    def current_period_bounds(self, at=None):
        """Non-mutating: returns (effective_period_started_at, period_ends_at)
        for `at` (default now). An idle account rolls forward to the period
        containing `at` in one step (integer division), not via N sequential
        resets -- shared by the read-only balance check and the mutating
        rollover so the boundary math lives in exactly one place."""
        at = at or timezone.now()
        duration = self._period_timedelta()
        elapsed = at - self.period_started_at
        if elapsed < duration:
            return self.period_started_at, self.period_started_at + duration
        periods_elapsed = elapsed // duration
        effective_start = self.period_started_at + periods_elapsed * duration
        return effective_start, effective_start + duration

    def remaining_tokens(self, at=None):
        """Read-only. Returns (remaining_tokens, period_ends_at) without
        saving -- safe to call outside a transaction for a pre-flight check
        or a page-render balance display."""
        start, ends_at = self.current_period_bounds(at)
        used = self.tokens_used if start == self.period_started_at else 0
        return max(self.token_budget - used, 0), ends_at

    def roll_period_if_needed(self, at=None):
        """Mutates self in place if the period has lapsed. Caller saves.
        Only ever called from record_spend() while holding a row lock
        (select_for_update) -- see gtm/ai_credits.py."""
        start, _ends_at = self.current_period_bounds(at)
        if start != self.period_started_at:
            self.period_started_at = start
            self.tokens_used = 0


class AICreditTransaction(models.Model):
    """Append-only audit trail of AI spend, mirroring the
    WorkspaceActivityEvent pattern in models_workspace.py. Not used for
    balance math (AICreditAccount.tokens_used is the source of truth) --
    exists for admin visibility / per-feature breakdown / debugging."""

    FEATURE_CHOICES = [
        ("playbook", "Playbook generation"),
        ("diagnostic", "Diagnostic insight"),
        ("diagnostic_batch", "Diagnostic insight (batch)"),
        ("enrichment", "Financial/competitor enrichment"),
        ("extract_tasks", "Task extraction from playbook"),
        ("context_note_rewrite", "Context note rewrite"),
        ("chat", "GTM Strategist chat"),
        ("workspace_agent", "Workspace agent chat"),
        ("team_chat", "Team chat"),
        ("action_item_completion", "Action item completion"),
        ("evidence_audit", "Strategic evidence audit"),
        ("resource_audit", "Resource library audit"),
        ("category_analysis", "Category document analysis"),
        ("delivery_analysis", "Delivery document analysis"),
        ("document_ocr", "Attachment OCR/text extraction"),
        ("gap_suggestions", "Gap analysis suggestions"),
        ("gap_action_items", "Gap-to-action-item breakdown"),
        ("client_summary", "Client summary draft"),
    ]

    id = models.BigAutoField(primary_key=True)
    account = models.ForeignKey(AICreditAccount, on_delete=models.CASCADE, related_name="transactions")
    session = models.ForeignKey(
        "gtm.AssessmentSession", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ai_credit_transactions",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ai_credit_transactions",
    )
    feature = models.CharField(max_length=32, choices=FEATURE_CHOICES)
    tokens_spent = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
            models.Index(fields=["feature", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.account}: {self.tokens_spent} tokens ({self.feature})"
