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

from django.db import transaction

from .models_ai_credits import AICreditAccount, AICreditTransaction


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


def can_spend(*, workspace=None, user=None, account: Optional[AICreditAccount] = None) -> CreditCheck:
    """Read-only pre-flight check -- call BEFORE making a Gemini call (or
    before spawning a background thread that will)."""
    account = account or resolve_account(workspace=workspace, user=user)
    if account is None:
        return CreditCheck(allowed=False, account=None, remaining=0, reset_at=None, reason="no_identity")
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
