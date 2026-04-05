# gtm/utils_ai_monitoring.py
"""
AI Usage Monitoring Utilities
------------------------------
Track and monitor Gemini API usage against quotas.
"""

import logging
from datetime import datetime, timedelta
from django.core.cache import cache

logger = logging.getLogger(__name__)

# Vertex AI Enterprise Tier Limits (Conservative Estimates)
FREE_TIER_LIMITS = {
    'requests_per_minute': 60,
    'requests_per_day': 2000,
    'tokens_per_minute': 10_000_000,
    'tokens_per_day': 200_000_000,
}

class AIUsageTracker:
    """Track AI API usage to prevent quota exhaustion."""
    
    CACHE_PREFIX = 'ai_usage'
    
    @classmethod
    def log_usage(cls, tokens_used: int, request_type: str = 'general'):
        """
        Log AI usage and check against limits.
        
        Args:
            tokens_used: Number of tokens consumed
            request_type: Type of request (playbook, diagnostic, chat)
        """
        now = datetime.now()
        minute_key = f"{cls.CACHE_PREFIX}:minute:{now.strftime('%Y%m%d%H%M')}"
        day_key = f"{cls.CACHE_PREFIX}:day:{now.strftime('%Y%m%d')}"
        
        # Helper for atomic increment with fallback
        def safe_incr(key, amount, timeout):
            try:
                # Attempt atomic increment
                return cache.incr(key, amount)
            except (ValueError, KeyError):
                # Key doesn't exist, set initial value
                cache.set(key, amount, timeout)
                return amount

        # Increment counters atomically
        minute_requests = safe_incr(f"{minute_key}:requests", 1, 60)
        minute_tokens = safe_incr(f"{minute_key}:tokens", tokens_used, 60)
        day_requests = safe_incr(f"{day_key}:requests", 1, 86400)
        day_tokens = safe_incr(f"{day_key}:tokens", tokens_used, 86400)
        
        # Log current usage
        logger.info(
            f"AI Usage | Type: {request_type} | "
            f"This call: {tokens_used} tokens | "
            f"Minute: {minute_requests}/{FREE_TIER_LIMITS['requests_per_minute']} requests, "
            f"{minute_tokens:,}/{FREE_TIER_LIMITS['tokens_per_minute']:,} tokens | "
            f"Today: {day_requests}/{FREE_TIER_LIMITS['requests_per_day']} requests, "
            f"{day_tokens:,} tokens"
        )
        
        # Check if approaching limits
        if minute_requests >= FREE_TIER_LIMITS['requests_per_minute'] * 0.8:
            logger.warning(f"⚠️ Approaching minute request limit: {minute_requests}/15")
        
        if day_requests >= FREE_TIER_LIMITS['requests_per_day'] * 0.8:
            logger.warning(f"⚠️ Approaching daily request limit: {day_requests}/1500")
        
        return {
            'minute': {'requests': minute_requests, 'tokens': minute_tokens},
            'day': {'requests': day_requests, 'tokens': day_tokens},
        }
    
    @classmethod
    def get_current_usage(cls):
        """Get current usage statistics."""
        now = datetime.now()
        minute_key = f"{cls.CACHE_PREFIX}:minute:{now.strftime('%Y%m%d%H%M')}"
        day_key = f"{cls.CACHE_PREFIX}:day:{now.strftime('%Y%m%d')}"
        
        return {
            'minute': {
                'requests': cache.get(f"{minute_key}:requests", 0),
                'tokens': cache.get(f"{minute_key}:tokens", 0),
                'limit_requests': FREE_TIER_LIMITS['requests_per_minute'],
                'limit_tokens': FREE_TIER_LIMITS['tokens_per_minute'],
            },
            'day': {
                'requests': cache.get(f"{day_key}:requests", 0),
                'tokens': cache.get(f"{day_key}:tokens", 0),
                'limit_requests': FREE_TIER_LIMITS['requests_per_day'],
            },
        }
    
    @classmethod
    def can_make_request(cls) -> tuple[bool, str]:
        """
        Check if we can make another API request without hitting limits.
        
        Returns:
            (can_proceed, reason)
        """
        usage = cls.get_current_usage()
        
        if usage['minute']['requests'] >= FREE_TIER_LIMITS['requests_per_minute']:
            return False, "Minute request limit reached (15 RPM)"
        
        if usage['day']['requests'] >= FREE_TIER_LIMITS['requests_per_day']:
            return False, "Daily request limit reached (1,500)"
        
        if usage['minute']['tokens'] >= FREE_TIER_LIMITS['tokens_per_minute']:
            return False, "Minute token limit reached (1M tokens)"
        
        return True, "OK"
