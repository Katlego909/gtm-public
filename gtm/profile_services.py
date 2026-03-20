# gtm/profile_services.py
"""
Service layer for User Profile management, gating, and credit tracking.
Follows Separation of Concerns by keeping logic out of models and views.
"""

from django.db import transaction
from .models import Profile

def get_or_create_profile(user):
    """Safely get or create a profile for a user."""
    profile, created = Profile.objects.get_or_create(user=user)
    return profile

def can_start_assessment(user):
    """
    Business Logic: Check if a user is allowed to start a new assessment.
    Used for gating lead generation and protecting AI costs.
    """
    if not user.is_authenticated:
        return False
    
    profile = get_or_create_profile(user)
    return profile.has_ai_credits

@transaction.atomic
def deduct_ai_credit(user):
    """
    Deduct 1 AI credit from the user's profile.
    Only deducts for 'free' tier users; Pro/Business users have unlimited runs 
    (or handled by a different cap logic later).
    """
    profile = get_or_create_profile(user)
    
    # Only deduct if not a premium user
    if profile.tier == 'free' and not profile.is_simulated_pro:
        if profile.ai_credits > 0:
            profile.ai_credits -= 1
            profile.save()
            return True
        return False
    
    # Premium users don't deduct (for now)
    return True

def simulate_upgrade(user, tier='pro'):
    """
    Simulate a subscription upgrade for testing/demo purposes.
    """
    profile = get_or_create_profile(user)
    profile.tier = tier
    profile.is_simulated_pro = True
    profile.save()
    return profile

def get_user_tier_label(user):
    """Returns a readable label for the user's current tier."""
    profile = get_or_create_profile(user)
    return profile.get_tier_display()
