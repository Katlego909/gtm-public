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
from .utils_logging import log_ai_error
import re
import markdown as md
from django.utils.safestring import mark_safe

logger = logging.getLogger(__name__)

# Import AI usage tracker
try:
    from .utils_ai_monitoring import AIUsageTracker
    MONITORING_AVAILABLE = True
except ImportError:
    MONITORING_AVAILABLE = False
    
def _normalize_ai_playbook_markdown(playbook_text: str) -> str:
    """
    Normalizes and cleans AI-generated markdown text for consistent rendering.
    Assumes Gemini’s mixed formatting.
    """
    if not playbook_text:
        return ""

    src = playbook_text.replace("\r\n", "\n").strip()

    # 1️⃣ Convert inline " * " separators into proper bullet lines
    src = re.sub(r"\s\*\s+", "\n- ", src)

    # 2️⃣ Make "Week X:" style lines into Markdown headings for consistency
    src = re.sub(r"(?m)^(Week\s+\d+:[^\n]*)$", r"### \1", src)

    # 3️⃣ Add blank lines before list, numbered, and heading items for proper block rendering
    src = re.sub(r"(?m)(?<!\n)\n(?=(?:- |\d+\. |#{1,6}\s))", "\n\n", src)

    # 4️⃣ Clean up extra spaces/newlines
    src = re.sub(r"[ \t]+\n", "\n", src)
    src = re.sub(r"\n{3,}", "\n\n", src)

    return src

def _extract_tasks_from_playbook_regex(playbook_text: str) -> list:
    """
    Extracts actionable tasks from AI playbook markdown/text using regex.
    Returns a list of clean, professional task strings.
    This is a fallback if AI generation of tasks fails.
    """
    if not playbook_text:
        return []
        
    tasks = []
    lines = playbook_text.splitlines()
    
    # Skip patterns that are clearly not tasks
    skip_patterns = [
        r'^\*\*Actions?\*\*$',  # **Actions:** or **Action:**
        r'^\*\*Objectives?\*\*$',  # **Objective:** or **Objectives:**
        r'^\*\*Week\s+\d+.*\*\*$',  # **Week 1:** etc
        r'^\*\*Day\s+\d+.*\*\*$',  # **Day 1-2:** etc  
        r'^\*\*Deliverables?\*\*$',  # **Deliverable:** etc
        r'^\*\*Key\s+Results?\*\*$',  # **Key Results:** etc
        r'^#{1,6}\s',  # Markdown headers
        r'^\s*$',  # Empty lines
        r'^.*:\s*$',  # Lines ending with just a colon
    ]
    
    for line in lines:
        line = line.strip()
        
        # Skip section headers and empty content
        if any(re.match(pattern, line, re.IGNORECASE) for pattern in skip_patterns):
            continue
        
        # Clean up markdown formatting
        cleaned_line = line
        cleaned_line = re.sub(r'\*\*(.*?)\*\*', r'\1', cleaned_line)  # Remove **bold**
        cleaned_line = re.sub(r'\*(.*?)\*', r'\1', cleaned_line)  # Remove *italics*
        cleaned_line = re.sub(r'^[-*]\s+', '', cleaned_line)  # Remove bullet points
        cleaned_line = re.sub(r'^\d+\.\s+', '', cleaned_line)  # Remove numbered lists
        cleaned_line = cleaned_line.strip()
        
        if not cleaned_line:
            continue
        
        # Extract meaningful tasks from sentences
        if '.' in cleaned_line:
            sentences = re.split(r'\.\s+(?=[A-Z])', cleaned_line)
            for sentence in sentences:
                sentence = sentence.strip().rstrip('.')
                
                # Look for action verbs and meaningful content
                action_patterns = [
                    r'^(Define|Create|Develop|Identify|Update|Build|Set|Establish|Implement|Review|Test|Launch|Execute|Draft|Document|Analyze|Optimize|Configure|Install|Conduct|Organize|Schedule|Plan|Design|Research)',
                    r'^(Convene|Interview|Survey|Contact|Reach out|Follow up|Send|Email|Call|Meet|Discuss)',
                    r'^(Gather|Collect|Compile|Prepare|Generate|Produce|Publish|Share|Distribute)',
                    r'^(Streamline|Improve|Enhance|Refine|Standardize|Automate|Integrate)'
                ]
                
                if any(re.match(pattern, sentence, re.IGNORECASE) for pattern in action_patterns):
                    if len(sentence) > 20 and len(sentence) < 150:  # Reasonable task length
                        # Capitalize first letter and ensure it ends properly
                        formatted_task = sentence[0].upper() + sentence[1:] if sentence else ""
                        if formatted_task and not formatted_task.endswith('.'):
                            formatted_task += '.'
                        tasks.append(formatted_task)
        else:
            # Single line task
            if len(cleaned_line) > 20 and len(cleaned_line) < 150:
                action_patterns = [
                    r'^(Define|Create|Develop|Identify|Update|Build|Set|Establish|Implement|Review|Test|Launch|Execute|Draft|Document|Analyze|Optimize|Configure|Install|Conduct|Organize|Schedule|Plan|Design|Research)',
                    r'^(Convene|Interview|Survey|Contact|Reach out|Follow up|Send|Email|Call|Meet|Discuss)',
                    r'^(Gather|Collect|Compile|Prepare|Generate|Produce|Publish|Share|Distribute)',
                    r'^(Streamline|Improve|Enhance|Refine|Standardize|Automate|Integrate)'
                ]
                
                if any(re.match(pattern, cleaned_line, re.IGNORECASE) for pattern in action_patterns):
                    formatted_task = cleaned_line[0].upper() + cleaned_line[1:] if cleaned_line else ""
                    if formatted_task and not formatted_task.endswith('.'):
                        formatted_task += '.'
                    tasks.append(formatted_task)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_tasks = []
    for task in tasks:
        task_lower = task.lower()
        if task_lower not in seen and len(task) > 30:  # Ensure substantial tasks
            seen.add(task_lower)
            unique_tasks.append(task)
    
    return unique_tasks[:8]  # Limit to 8 high-quality tasks


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
        log_ai_error("Gemini initialization", e, service="google", model="gemini-2.5-flash")
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

    # Fetch context notes for this session
    context_notes = []
    if hasattr(snapshot, 'session') and snapshot.session:
        responses = Response.objects.filter(session=snapshot.session).exclude(context_note="").select_related('question')
        for r in responses:
            context_notes.append(f"{r.question.text}: {r.context_note}")

    context_notes_text = "\n".join(context_notes)

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

Additional Context Provided by User:
{context_notes_text if context_notes_text else 'No extra context provided.'}
"""
    return prompt


# ================================================================
# MAIN GENERATION FUNCTION
# ================================================================
def generate_playbook_with_gemini(snapshot: ResultSnapshot) -> str:
    """
    Generate a personalized GTM playbook using Gemini, with fallbacks.
    Also creates GapAnalysisMetric records from AI response.
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
                # Log token usage
                if hasattr(response, 'usage_metadata'):
                    usage = response.usage_metadata
                    total_tokens = usage.total_token_count
                    logger.info(
                        f"✅ AI playbook generated for {snapshot.company_name} | "
                        f"Tokens: {usage.prompt_token_count} input + {usage.candidates_token_count} output = {total_tokens} total"
                    )
                    # Track usage against quotas
                    if MONITORING_AVAILABLE:
                        AIUsageTracker.log_usage(total_tokens, 'playbook')
                else:
                    logger.info(f"✅ AI playbook generated for {snapshot.company_name}")
        except Exception as e:
            log_ai_error(
                "Playbook generation",
                e,
                service="google",
                model="gemini-2.5-flash",
                prompt=prompt,
                extra={"snapshot_id": snapshot.id},
            )

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
            
            # Log token usage
            if hasattr(ai_response, 'usage_metadata'):
                usage = ai_response.usage_metadata
                total_tokens = usage.total_token_count
                logger.info(
                    f"✅ Diagnostic insight for {response.question.id_code} | "
                    f"Tokens: {total_tokens}"
                )
                # Track usage against quotas
                if MONITORING_AVAILABLE:
                    AIUsageTracker.log_usage(total_tokens, 'diagnostic')
            else:
                logger.info(f"✅ Diagnostic insight generated for {response.question.id_code}")
            
    except Exception as e:
        log_ai_error(
            "Diagnostic insight generation",
            e,
            service="google",
            model="gemini-2.5-flash",
            prompt=prompt,
            extra={"response_id": response.id},
        )
        
        # Fallback to static diagnostic note if AI fails
        if response.question.diagnostic_note:
            text = response.question.diagnostic_note
            response.ai_insight = text
            response.save(update_fields=["ai_insight"])
            logger.info(f"📝 Using static diagnostic for {response.question.id_code}")
        
    return text


def generate_concise_action_items(playbook_text: str) -> list:
    """
    Extracts and summarizes actionable items from the playbook text using AI.
    Returns a list of concise action strings.
    """
    model = _init_gemini()
    if not model:
        return []

    prompt = f"""
    Extract the key action items from the following GTM playbook text.
    Summarize each action item into a concise, actionable sentence (max 15 words).
    Return the result as a simple list of strings, one per line.
    Do not use bullet points or numbering in the output.
    
    Playbook Text:
    {playbook_text[:8000]}
    """

    try:
        response = model.generate_content(prompt)
        text = response.text.strip()
        # Split by newlines and filter empty lines
        actions = [line.strip() for line in text.split('\\n') if line.strip()]
        
        # Log usage
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            total_tokens = usage.total_token_count
            if MONITORING_AVAILABLE:
                AIUsageTracker.log_usage(total_tokens, 'action_extraction')
                
        return actions
    except Exception as e:
        log_ai_error(
            "Action item extraction",
            e,
            service="google",
            model="gemini-2.5-flash",
            prompt=prompt,
        )
        return []
