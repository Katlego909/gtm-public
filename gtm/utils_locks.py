# gtm/utils_locks.py
"""
DB-backed keyed lock/cooldown primitives -- see AIGenerationLock's
docstring (gtm/models_ai_locks.py) for why this exists instead of using
Django's cache. Mirrors the acquire/release/peek shape of cache.add() /
cache.delete() / cache.get() so callers (gtm/ai_services.py's
_acquire_lock/_release_lock/_quota_cooldown_active/_set_quota_cooldown)
can delegate here as thin wrappers without changing their own signatures.
"""
from datetime import timedelta

from django.utils import timezone

from .models_ai_locks import AIGenerationLock


def acquire(key: str, ttl_seconds: int) -> bool:
    """cache.add() equivalent: True iff the caller now holds the lock.

    Two statements, no transaction or savepoint (this runs on polling
    requests and the results page):
      1. make sure a row exists -- inserted already-expired, and a no-op if
         the key is present (INSERT ... ON CONFLICT DO NOTHING);
      2. claim it with one conditional UPDATE that only matches an expired
         row. A single UPDATE is atomic per row, so of any number of
         concurrent callers exactly one sees rowcount 1.
    """
    now = timezone.now()
    AIGenerationLock.objects.bulk_create(
        [AIGenerationLock(lock_key=key, expires_at=now - timedelta(seconds=1))],
        ignore_conflicts=True,
    )
    claimed = AIGenerationLock.objects.filter(lock_key=key, expires_at__lte=now).update(
        expires_at=now + timedelta(seconds=ttl_seconds), updated_at=now,
    )
    return claimed == 1


def release(key: str) -> None:
    """cache.delete() equivalent."""
    AIGenerationLock.objects.filter(lock_key=key).delete()


def is_active(key: str) -> bool:
    """Read-only peek -- cache.get() + expiry-compare equivalent. Used both
    to check a cooldown flag and to tell whether a lock is still actually
    held (e.g. to distinguish a live "generating" status from one left
    behind by a thread that died before releasing its lock)."""
    expires_at = (
        AIGenerationLock.objects.filter(lock_key=key)
        .values_list("expires_at", flat=True)
        .first()
    )
    return bool(expires_at and expires_at > timezone.now())


def set_active_until(key: str, ttl_seconds: int) -> None:
    """cache.set() equivalent (unconditional) -- used for the global quota-
    cooldown flag, which isn't acquire/release, just "active until X"."""
    AIGenerationLock.objects.update_or_create(
        lock_key=key,
        defaults={"expires_at": timezone.now() + timedelta(seconds=ttl_seconds)},
    )
