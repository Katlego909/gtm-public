# gtm/ai_services.py
"""
AI Services for the Funti3r GTM Validator
-----------------------------------------
Handles AI-driven playbook generation using Gemini.
Falls back to static RecommendationBand content if AI is unavailable.
"""

import logging
from django.conf import settings
from django.utils.html import strip_tags

from .models import ResultSnapshot, RecommendationBand

logger = logging.getLogger(__name__)

# ================================================================
# TRY IMPORTING GEMINI CLIENT (SAFE IMPORT)
# ================================================================
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logger.warning("Gemini client not installed — AI playbook generation disabled.")


# ================================================================
# INITIALIZE GEMINI CONFIG (SAFE)
# ================================================================
def _init_gemini():
    """Safely initialize Gemini with API key if available."""
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not GEMINI_AVAILABLE or not api_key:
        return None
    try:
        genai.configure(api_key=api_key)
        # ✅ Explicitly use the new stable model
        return genai.GenerativeModel("models/gemini-2.5-flash")
    except Exception as e:
        logger.error(f"Gemini initialization failed: {e}")
        return None


# ================================================================
# PROMPT GENERATOR
# ================================================================
def _build_prompt(snapshot: ResultSnapshot) -> str:
    """Create a structured prompt for AI to generate a personalized GTM playbook."""
    data = {
        "company_name": snapshot.company_name or "Unnamed Company",
        "industry": snapshot.industry or "Unknown Industry",
        "overall": snapshot.overall,
        "stage": snapshot.band.stage if snapshot.band else "Unspecified",
        "headline": snapshot.band.headline if snapshot.band else "",
        "categories": snapshot.category_breakdown or [],
    }

    cat_lines = "\n".join(
        [f"- {c['category']}: {c['avg']}/5" for c in data["categories"]]
    )

    prompt = f"""
You are a Go-To-Market strategy consultant.

Create a **personalized 30-day GTM improvement playbook** for the company below.

Include:
- A short diagnostic summary (tone: helpful, professional)
- Top 3 priority areas to focus on
- 4-week action plan with weekly objectives
- Success metrics (quantifiable goals)
- Optional tool or process recommendations (if relevant)

Company: {data['company_name']}
Industry: {data['industry']}
Stage: {data['stage']}
GTM Score: {data['overall']}
Summary: {data['headline']}

Category Averages:
{cat_lines}

Respond in clean markdown for rendering inside the Playbook page.
    """.strip()

    return prompt


# ================================================================
# MAIN GENERATION FUNCTION
# ================================================================
def generate_playbook_with_gemini(snapshot: ResultSnapshot) -> str:
    """
    Generate a personalized GTM playbook using Gemini.
    Falls back to RecommendationBand.actions_markdown if AI is unavailable or fails.
    """

    # ---- 1️⃣ Attempt Gemini generation
    model = _init_gemini()
    if model:
        prompt = _build_prompt(snapshot)
        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            if text:
                logger.info(f"✅ AI playbook generated for {snapshot.company_name}")
                return text
        except Exception as e:
            logger.error(f"Gemini generation failed: {e}")

    # ---- 2️⃣ Fallback to static recommendation
    logger.warning("⚠️ Falling back to static RecommendationBand playbook.")
    band = snapshot.band or RecommendationBand.objects.filter(
        min_score__lte=snapshot.overall, max_score__gte=snapshot.overall
    ).first()

    if band and band.actions_markdown:
        return strip_tags(band.actions_markdown)

    return (
        "No AI-generated playbook available yet.\n\n"
        "We recommend focusing on your lowest-rated GTM categories first."
    )
