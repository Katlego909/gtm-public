# gtm/models_ai_locks.py
"""
DB-backed replacement for cache.add()/cache.delete() mutual exclusion and
cache.set()/cache.get() TTL flags used around Gemini generation (playbook,
diagnostic insights, enrichment) and a handful of other AI-adjacent
background jobs (action-item AI completion, insights digest).

Same reasoning as gtm/models_ai_credits.py: CACHES isn't configured in
settings.py (Django defaults to per-process LocMemCache) and the app
deploys to Cloud Run, which can run multiple instances/processes -- a
cache-backed lock or cooldown flag doesn't actually coordinate across
them, so two instances can both start generating the same content, and a
quota cooldown set by one instance is invisible to its siblings.

One row per distinct lock_key, upserted across acquire/release cycles --
table size is bounded by the number of distinct keys concurrently or
recently in use, not by the number of acquisition attempts.
"""
from django.db import models


class AIGenerationLock(models.Model):
    lock_key = models.CharField(max_length=200, primary_key=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.lock_key} (expires {self.expires_at.isoformat()})"
