# gtm/ai_credits.py
"""
Per-workspace/per-user Gemini token budget: the real spend-control layer.

Distinct from gtm/utils_ai_monitoring.py's AIUsageTracker, which is a
global, cache-backed, purely observational counter against Google's own
quota ceiling (kept as-is, unchanged, for that separate purpose). This
module is DB-backed, because CACHES is not configured (Django defaults to
per-process LocMemCache) and the app runs on Cloud Run, which can scale to
multiple instances/processes -- a cache-backed budget would silently
under-count spend across instances.

Every AI call site in the app is reachable only from an already-
authenticated request (every view that can trigger a Gemini call is
@login_required or funnels through safe_get_session_or_403, which itself
requires request.user.is_authenticated) -- so resolve_account never needs
to handle a genuinely anonymous caller. It still fails closed (returns
None / blocks) if it's ever handed no identity at all, as cheap defense in
depth rather than a live code path.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from . import utils_locks
from .models_ai_credits import AICreditAccount, AICreditTransaction

# Once-per-day dedupe key for the global-cap breach alert email (see
# _maybe_alert_global_cap_exceeded) -- reuses the DB-backed lock primitives
# built for AI generation locking (gtm/utils_locks.py) purely as a "have we
# already sent this today" flag, not for mutual exclusion.
GLOBAL_CAP_ALERT_DEDUPE_KEY = "gtm:ai:global_cap_alert_sent"


@dataclass
class CreditCheck:
    allowed: bool
    account: Optional[AICreditAccount]
    remaining: int
    reset_at: Optional[datetime]
    reason: str = ""  # "no_identity" | "exhausted" | "" (allowed)


def resolve_account(*, workspace=None, user=None) -> Optional[AICreditAccount]:
    """Workspace pool takes priority over the personal pool. Returns None
    if neither is resolvable; callers must treat that as blocked, not as
    unlimited."""
    if workspace is not None:
        account, _ = AICreditAccount.objects.get_or_create(
            workspace=workspace,
            defaults={"token_budget": AICreditAccount.DEFAULT_WORKSPACE_TOKEN_BUDGET},
        )
        return account
    if user is not None and getattr(user, "is_authenticated", False):
        account, _ = AICreditAccount.objects.get_or_create(
            user=user,
            defaults={"token_budget": AICreditAccount.DEFAULT_PERSONAL_TOKEN_BUDGET},
        )
        return account
    return None


def resolve_account_for_session(session, user=None) -> Optional[AICreditAccount]:
    """Convenience for the common AssessmentSession-shaped call sites.
    `user` should be passed explicitly when the caller has request.user --
    session.user can be stale/None for a workspace-shared session."""
    if session is None:
        return resolve_account(user=user)
    workspace = getattr(session, "workspace", None)
    resolved_user = user or getattr(session, "user", None)
    return resolve_account(workspace=workspace, user=resolved_user)


def _global_daily_spend_exceeded() -> bool:
    """Aggregate ceiling across every account combined -- AICreditAccount's
    per-account budgets bound one workspace/user's spend, but nothing bounds
    the *total* across all of them, and workspace creation was unrestricted
    (see gtm/views_workspace.py's MAX_WORKSPACES_PER_USER cap for the other
    half of this fix: how many accounts can exist in the first place).
    AI_GLOBAL_DAILY_TOKEN_CAP unset/0 means "no global cap" -- opt-in, since
    existing single-tenant/internal usage shouldn't suddenly start failing.
    """
    cap = getattr(settings, "AI_GLOBAL_DAILY_TOKEN_CAP", 0)
    if not cap:
        return False
    start_of_day = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = AICreditTransaction.objects.filter(created_at__gte=start_of_day).aggregate(
        total=Sum("tokens_spent")
    )["total"] or 0
    return total >= cap


def _maybe_alert_global_cap_exceeded() -> None:
    """Fire at most one alert email per day when the global cap first trips
    -- utils_locks.is_active/set_active_until are reused here purely as a
    24h dedupe flag, not for their usual mutual-exclusion purpose."""
    if utils_locks.is_active(GLOBAL_CAP_ALERT_DEDUPE_KEY):
        return
    utils_locks.set_active_until(GLOBAL_CAP_ALERT_DEDUPE_KEY, 24 * 60 * 60)
    alert_to = getattr(settings, "BETA_ALERT_EMAIL", "")
    if not alert_to:
        return
    from .utils_email import send_global_ai_cap_alert_email
    send_global_ai_cap_alert_email(alert_to)


def can_spend(*, workspace=None, user=None, account: Optional[AICreditAccount] = None) -> CreditCheck:
    """Read-only pre-flight check -- call BEFORE making a Gemini call (or
    before spawning a background thread that will)."""
    account = account or resolve_account(workspace=workspace, user=user)
    if account is None:
        return CreditCheck(allowed=False, account=None, remaining=0, reset_at=None, reason="no_identity")
    if _global_daily_spend_exceeded():
        _maybe_alert_global_cap_exceeded()
        return CreditCheck(allowed=False, account=account, remaining=0, reset_at=None, reason="global_cap")
    remaining, reset_at = account.remaining_tokens()
    return CreditCheck(
        allowed=remaining > 0, account=account, remaining=remaining, reset_at=reset_at,
        reason="" if remaining > 0 else "exhausted",
    )


def format_reset_time(reset_at) -> str:
    """Portable 12-hour time format (e.g. "3:00 PM") without relying on the
    platform-specific %-I/%#I strftime flags, which differ between Linux
    (Cloud Run, prod) and Windows (local dev)."""
    if not reset_at:
        return ""
    return reset_at.strftime("%I:%M %p").lstrip("0")


def record_spend(account: Optional[AICreditAccount], tokens_used: int, feature: str, *,
                  session=None, actor=None) -> None:
    """Debit actual tokens after a Gemini call returns usage_metadata. Safe
    under concurrent requests/instances via select_for_update(); rolls the
    period forward first if it has lapsed (no cron exists -- lazy reset
    only). No-op if account is None or tokens_used <= 0, so call sites can
    call this unconditionally without an extra guard."""
    if not account or not tokens_used or tokens_used <= 0:
        return
    with transaction.atomic():
        locked = AICreditAccount.objects.select_for_update().get(pk=account.pk)
        locked.roll_period_if_needed()
        locked.tokens_used += tokens_used
        locked.save(update_fields=["tokens_used", "period_started_at", "updated_at"])
        AICreditTransaction.objects.create(
            account=locked, session=session, feature=feature,
            tokens_spent=tokens_used, actor=actor,
        )
