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

from .models import ResultSnapshot, RecommendationBand, AssessmentSession, Question, Response

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
        return genai.GenerativeModel("gemini-2.5-flash")
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
    Generate a personalized GTM playbook using Gemini, with fallbacks.
    Persists the result (AI or fallback) to the database once.
    """

    final_playbook_text = ""

    # ---- 1️⃣ Attempt Gemini generation
    model = _init_gemini()
    if model:
        prompt = _build_prompt(snapshot)
        try:
            response = model.generate_content(prompt)
            text = response.text.strip()
            if text:
                final_playbook_text = text
                logger.info(f"✅ AI playbook generated for {snapshot.company_name}")
        except Exception as e:
            logger.error(f"Gemini generation failed: {e}")

    # ---- 2️⃣ Fallback to static recommendation (only if AI failed or was disabled)
    if not final_playbook_text:
        logger.warning("⚠️ Falling back to static RecommendationBand playbook.")
        
        # Use snapshot.band first, or look it up if it's missing (safer)
        band = snapshot.band
        if not band and snapshot.overall is not None:
            band = RecommendationBand.objects.filter(
                min_score__lte=snapshot.overall, max_score__gte=snapshot.overall
            ).first()

        if band and band.actions_markdown:
            final_playbook_text = strip_tags(band.actions_markdown)

    # ---- 3️⃣ Final generic fallback (if no content was found at all)
    if not final_playbook_text:
        final_playbook_text = (
            "No AI-generated playbook available yet.\n\n"
            "We recommend focusing on your lowest-rated GTM categories first."
        )
        
    # 🌟 CONSOLIDATED SAVE: Persist the final content once
    snapshot.ai_playbook = final_playbook_text
    snapshot.save(update_fields=["ai_playbook"])

    return final_playbook_text


# ================================================================
# DIAGNOSTIC PROMPT GENERATOR
# ================================================================
def _build_diagnostic_prompt(session: AssessmentSession, question: Question, score: int) -> str:
    """Create a structured prompt for AI to generate a question-level diagnostic."""
    
    # Contextual data
    company_name = session.company_name or "a B2B company"
    industry = session.industry or "a general industry"
    stage = session.snapshot.band_stage if getattr(session, 'snapshot', None) and session.snapshot.band_stage else "Unspecified"
    
    # Score severity mapping
    severity = {1: "Critical Failure", 2: "Serious Gap", 3: "Improvement Needed"}.get(score, "Low Priority")

    prompt = f"""
You are a highly experienced Go-To-Market consultant. Your task is to provide a brief, actionable diagnostic insight for a single low-scoring area.

Company Context:
- Company Name: {company_name}
- Industry: {industry}
- GTM Stage: {stage}
- Question ID: {question.id_code}
- Question Text: {question.text}
- User Score: {score}/5.0 (Severity: {severity})
- Static Note (for context only): {question.diagnostic_note or 'N/A'}

Task: Generate a single, concise paragraph (max 5 sentences) that explains the **immediate risk** or **consequence** of this low score in the context of the company's industry and stage. Do NOT provide a full action plan; keep the focus on the "WHY this matters now."

Respond in clean, professional prose.
    """.strip()

    return prompt

# ================================================================
# MAIN DIAGNOSTIC FUNCTION
# ================================================================
def generate_diagnostic_insight(response: Response) -> str:
    """
    Generates and saves a granular diagnostic insight for a specific low-scored response.
    Returns the generated text.
    """
    
    # Only generate for low scores (1 or 2) where the insight is critical
    if response.score > 3:
        return "" 
        
    model = _init_gemini()
    if not model:
        logger.warning("Gemini client unavailable for diagnostic insight.")
        return ""

    prompt = _build_diagnostic_prompt(response.session, response.question, response.score)
    text = ""
    try:
        # Use model to generate content
        ai_response = model.generate_content(prompt)
        text = ai_response.text.strip()
        
        if text:
            # 🌟 Save the insight directly to the Response object
            response.ai_insight = text
            response.save(update_fields=["ai_insight"])
            logger.info(f"✅ Diagnostic insight generated for {response.question.id_code}")
            
    except Exception as e:
        logger.error(f"Gemini diagnostic generation failed for {response.question.id_code}: {e}")
        
        # Fallback to static diagnostic note if AI fails
        if response.question.diagnostic_note:
            text = response.question.diagnostic_note
            response.ai_insight = text
            response.save(update_fields=["ai_insight"])
            logger.info(f"📝 Using static diagnostic for {response.question.id_code}")
        
    return text
