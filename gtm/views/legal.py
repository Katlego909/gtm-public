from django.shortcuts import render

from ..models_ai_credits import AICreditAccount


def privacy_policy(request):
    return render(request, "gtm/privacy.html")


def terms_of_service(request):
    return render(request, "gtm/terms.html")


def contact(request):
    return render(request, "gtm/contact.html")


def pricing(request):
    """Public pricing page. Tiers differ only by the monthly AI-credit
    allowance (see the pricing-tiers decision); credit numbers are pulled
    from AICreditAccount so the page can't drift from the enforced budgets.
    Prices are page-level marketing data -- the model stores no price because
    there is no billing/checkout yet (paid CTAs are a waitlist mailto)."""
    A = AICreditAccount
    tiers = [
        {
            "name": "Free",
            "price": 0,
            "tagline": "Evaluate your go-to-market readiness end to end.",
            "credits": A.tokens_to_credits(A.budget_for_tier(A.TIER_FREE)),
            "cta": "start",
            "highlight": False,
        },
        {
            "name": "Pro",
            "price": 29,
            "tagline": "For teams running GTM week to week.",
            "credits": A.tokens_to_credits(A.budget_for_tier(A.TIER_PRO)),
            "cta": "waitlist",
            "highlight": True,
        },
        {
            "name": "Scale",
            "price": 99,
            "tagline": "For agencies and heavy, multi-client use.",
            "credits": A.tokens_to_credits(A.budget_for_tier(A.TIER_SCALE)),
            "cta": "waitlist",
            "highlight": False,
        },
    ]
    return render(request, "gtm/pricing.html", {
        "tiers": tiers,
        "tokens_per_credit": A.TOKENS_PER_CREDIT,
    })
