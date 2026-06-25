# gtm/ai_services.py
"""
AI Services for the Funti3r GTM Validator
-----------------------------------------
Handles AI-driven playbook generation using Gemini.
Falls back to static RecommendationBand content if AI is unavailable.
"""

import logging
import time
from django.conf import settings
from django.core.cache import cache
from django.utils.html import strip_tags

from .models import ResultSnapshot, RecommendationBand, AssessmentSession, Question, Response
from .utils_logging import log_ai_error
import re
import json
from .scoring_engine import build_recommendation_context

logger = logging.getLogger(__name__)

AI_QUOTA_COOLDOWN_CACHE_KEY = "gtm:ai:gemini:quota_cooldown_until"
AI_REQUEST_COUNTER_CACHE_KEY = "gtm:ai:gemini:req_count:60s"
AI_LOCK_TTL_SECONDS = 120
AI_REQUEST_WINDOW_SECONDS = 60

# Module-level client cache to avoid repeated initialization
_cached_client = None
_client_lock = __import__('threading').Lock()


def _clean_json_response(text: str) -> str:
    """
    Cleans AI-generated text to ensure it's a valid JSON string.
    Handles markdown code blocks, escape sequences, and common formatting quirks from Vertex AI.
    """
    if not text:
        return ""

    # 1. Remove Markdown code labels and strip spaces
    # Standard ```json label
    text = re.sub(r'^```json\s*', '', text.strip(), flags=re.MULTILINE | re.IGNORECASE)
    # Generic ``` label
    text = re.sub(r'^```\s*', '', text, flags=re.MULTILINE)
    # Closing ```
    text = re.sub(r'\s*```$', '', text, flags=re.MULTILINE)

    # 2. Extract the first { and last } to ignore any conversational chatter
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1:
        text = text[start:end+1]

    # 3. Fix common escape sequence issues from Vertex AI
    # Handle invalid escape sequences like \u (without 4 hex digits), \x, etc.
    # Replace with literal backslash to let JSON handle it
    text = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)  # Escape invalid backslashes

    return text.strip()


def _is_quota_error(error: Exception) -> bool:
    """Return True when the exception indicates Gemini quota/rate-limit exhaustion."""
    msg = str(error).lower()
    return (
        error.__class__.__name__ == "ResourceExhausted"
        or "quota exceeded" in msg
        or "resourceexhausted" in msg
        or "rate limit" in msg
    )


def _extract_retry_delay_seconds(error: Exception) -> int:
    """Extract retry delay from Gemini error text, defaulting to a conservative value."""
    msg = str(error)
    match = re.search(r"Please retry in\s+([0-9]+(?:\.[0-9]+)?)s", msg, flags=re.IGNORECASE)
    if match:
        try:
            return max(1, int(float(match.group(1))))
        except (ValueError, TypeError):
            pass
    return 60


def _set_quota_cooldown(retry_after_seconds: int):
    """Set a short global cooldown to prevent quota-storm retry loops."""
    cooldown_seconds = min(max(retry_after_seconds, 1), 300)
    cache.set(
        AI_QUOTA_COOLDOWN_CACHE_KEY,
        time.time() + cooldown_seconds,
        timeout=cooldown_seconds,
    )


def _quota_cooldown_active() -> bool:
    """Check if Gemini calls should be skipped temporarily due to recent quota errors."""
    until_ts = cache.get(AI_QUOTA_COOLDOWN_CACHE_KEY)
    return bool(until_ts and until_ts > time.time())


def _acquire_lock(lock_key: str, ttl_seconds: int = AI_LOCK_TTL_SECONDS) -> bool:
    """Acquire a cache lock to avoid duplicate concurrent AI calls."""
    return cache.add(lock_key, "1", timeout=ttl_seconds)

def _request_budget_available() -> bool:
    """Vertex AI enterprise quota is much higher; using more relaxed budget."""
    return True # Removed strict 4 RPM gate for Vertex AI


def _release_lock(lock_key: str):
    """Release a previously acquired cache lock."""
    cache.delete(lock_key)


# Metadata and monitoring tools
try:
    from .utils_ai_monitoring import AIUsageTracker
    MONITORING_AVAILABLE = True
except ImportError:
    MONITORING_AVAILABLE = False
    
def _normalize_ai_playbook_markdown(playbook_text: str) -> str:
    """
    Normalizes and cleans AI-generated markdown text for consistent rendering.
    Handles both raw markdown and JSON-wrapped strings.
    """
    if not playbook_text:
        return ""

    src = playbook_text.strip()

    # 🛠️ JSON DETECTION (Last Line of Defense)
    # If the text looks like it might contain a JSON object with our key
    if "markdown_playbook" in src:
        try:
            # Try to extract and clean the JSON part
            potential_json = _clean_json_response(src)
            parsed = json.loads(potential_json)
            if isinstance(parsed, dict) and "markdown_playbook" in parsed:
                src = parsed["markdown_playbook"]
        except (json.JSONDecodeError, Exception):
            # Regex fallback: extract content of "markdown_playbook" key
            # Standard: "markdown_playbook": "content"
            # We look for the start and then capture everything until a delimiter OR the end of string
            match = re.search(r'["\']markdown_playbook["\']\s*:\s*["\'](.*?)(?:["\']\s*,\s*["\']|["\']\s*}|$)', src, re.DOTALL)
            if match:
                src = match.group(1)

    # Ensure literal \n from LLM/JSON artifacts are converted to real newlines
    src = src.replace('\\n', '\n').replace('\\"', '"').replace("\r\n", "\n").strip()

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
# UNIFIED GENAI CLIENT (GCP VERTEX AI)
# ================================================================
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    logger.warning("google-genai client not installed — AI services disabled.")

# Generation configs to disable thinking and control token usage
_FAST_CONFIG = None      # diagnostics, rewrites: 600 tokens, no thinking
_PLAYBOOK_CONFIG = None  # playbook: 4096 tokens, no thinking
_ACTION_CONFIG = None    # action items: 512 tokens, no thinking

if GENAI_AVAILABLE:
    try:
        _FAST_CONFIG = types.GenerateContentConfig(
            temperature=0.4,
            max_output_tokens=600,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        _PLAYBOOK_CONFIG = types.GenerateContentConfig(
            temperature=0.6,
            max_output_tokens=8192,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        _ACTION_CONFIG = types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=512,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
    except Exception as e:
        logger.warning(f"Failed to create generation configs: {e}")

def _get_client():
    """Returns cached Google GenAI client, initializing once if needed."""
    global _cached_client

    if not GENAI_AVAILABLE:
        return None

    project_id = getattr(settings, "GCP_PROJECT_ID", None)
    if not project_id:
        return None

    if _cached_client is not None:
        return _cached_client

    with _client_lock:
        if _cached_client is not None:
            return _cached_client

        location = getattr(settings, "GCP_LOCATION", "us-central1")
        try:
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=location
            )
            _cached_client = client
            return client
        except Exception as e:
            log_ai_error("GenAI Client Initialization", e, service="google-genai")
            return None


# ================================================================
# PROMPT GENERATOR
# ================================================================
def _build_prompt(snapshot: ResultSnapshot) -> str:
    """
    Build a constrained, structured prompt for Gemini.

    The scoring engine (scoring_engine.py) runs first and identifies:
      - Top 3 highest-leverage dimensions (by opportunity_pts)
      - Named GTM failure patterns that fired for this company's scores

    Gemini's only job is to write industry- and size-specific prose around the
    structured findings — not to decide what the priorities are. This makes the
    output consistent, auditable, and genuinely personalised.
    """
    company_name  = snapshot.company_name  or "Unnamed Company"
    industry      = snapshot.industry      or "Unknown Industry"
    company_size  = snapshot.company_size  or "Not specified"
    revenue_range = snapshot.revenue_range or "Not specified"
    country       = snapshot.country       or "Not specified"
    crm           = snapshot.crm           or "Not specified"
    overall       = snapshot.overall
    stage         = snapshot.band.stage    if snapshot.band else "Unspecified"

    # ── 1. Get question-level scores and run the scoring engine ──────────────
    rec_ctx: dict = {}
    if hasattr(snapshot, "session") and snapshot.session:
        responses = Response.objects.filter(
            session=snapshot.session
        ).select_related("question")
        question_scores = {r.question.id_code: r.score for r in responses}
        if question_scores:
            rec_ctx = build_recommendation_context(question_scores)

    # ── 2. Build prompt sections from scoring engine output ──────────────────
    pillar_lines = "\n".join(
        f"  {p}: {avg:.2f}/5"
        for p, avg in rec_ctx.get("pillar_avgs", {}).items()
    ) or "  (pillar averages unavailable)"

    priority_lines = ""
    for i, pri in enumerate(rec_ctx.get("top_priorities", []), 1):
        priority_lines += (
            f"\n  {i}. {pri['dimension']} ({pri['pillar']}) — "
            f"current score {pri['current_score']}/5, "
            f"improving to 5/5 would add +{pri['opportunity_pts']:.1f} pts to overall score"
        )
    if not priority_lines:
        priority_lines = "\n  (priority data unavailable)"

    pattern_lines = ""
    for pat in rec_ctx.get("patterns", []):
        pattern_lines += (
            f"\n  [{pat['severity']}] {pat['headline']}\n"
            f"    Root cause: {pat['root_cause']}\n"
            f"    Suggested quick win: {pat['quick_win']}\n"
        )
    if not pattern_lines:
        pattern_lines = "\n  No critical patterns detected."

    total_opp = rec_ctx.get("total_opportunity_pts", "N/A")

    # ── 3. Context notes from the user ───────────────────────────────────────
    context_notes: list[str] = []
    if hasattr(snapshot, "session") and snapshot.session:
        for r in Response.objects.filter(
            session=snapshot.session
        ).exclude(context_note="").select_related("question"):
            context_notes.append(f"  [{r.question.id_code}] {r.context_note[:200]}")
    context_notes_text = "\n".join(context_notes) or "  None provided."

    prompt = f"""You are a Go-To-Market strategy consultant. A business has completed a GTM assessment.
The scoring engine has already identified the top priorities and structural failure patterns.
Your job is to write PERSONALISED implementation guidance — calibrated to this specific company's
industry, size, and region — using the structured findings provided below.
Do NOT ignore the priorities or invent different ones.

=== COMPANY PROFILE ===
Company:       {company_name}
Industry:      {industry}
Company Size:  {company_size}
Revenue Range: {revenue_range}
Region:        {country}
CRM:           {crm}
GTM Stage:     {stage}
Overall Score: {overall}/100
Points still available (if all questions improve to 5/5): {total_opp}

=== PILLAR AVERAGES ===
{pillar_lines}

=== TOP 3 HIGHEST-LEVERAGE PRIORITIES ===
(Ranked by points contributed to overall score — fix these first)
{priority_lines}

=== DETECTED GTM FAILURE PATTERNS ===
(Structural problems identified by the rules engine from individual question scores)
{pattern_lines}

=== ADDITIONAL CONTEXT FROM THE BUSINESS ===
{context_notes_text}

=== YOUR OUTPUT ===
Respond ONLY with a valid raw JSON object (no markdown code fences). Use these exact keys:

1. "markdown_playbook"
   Structure:
   ## Priority 1: [exact dimension name from priorities above]
   [2–3 sentences: why this specific gap matters for a {company_size} company in {industry} in {country}]
   **What to do in the next 30 days:**
   - [specific action 1 — concrete, not generic]
   - [specific action 2]
   - [specific action 3]
   **What success looks like:** [one measurable outcome]

   [Repeat for Priority 2 and Priority 3]

   ## Patterns to address
   [One short paragraph per Critical or High pattern — what it means for THIS company specifically]

   Rules: use the company's industry, size, and region to make every point concrete.
   Do NOT give generic advice that could apply to any company. Do NOT name competitors.

2. "financial_summary"
   150–200 word markdown section titled "## Financial Estimates".
   Quantify the cost of NOT addressing the top 3 priorities (pipeline leakage,
   churn cost, CAC waste — whichever apply). Calibrate to {revenue_range} revenue.
   End with: "These are directional estimates — validate with your finance team."

3. "competitor_analysis"
   150–200 word markdown section titled "## Competitive Position".
   How do {industry} companies in {country} typically perform on the detected weak dimensions?
   Where is this company behind versus ahead of regional peers?

4. "risk_status"
   One of: "High", "Medium", or "Low"

5. "learning_topics"
   Array of up to 3 specific GTM skill or concept strings to study next.
"""
    return prompt


def extract_tasks_from_playbook(playbook_text: str, company_name: str, n: int = 4) -> list[str]:
    """
    Ask Gemini to extract n short actionable tasks from an existing AI playbook.
    Returns a list of task strings (each <= 220 chars). Returns [] on any failure.
    """
    client = _get_client()
    if not client or not playbook_text:
        return []

    prompt = f"""You are extracting action items from a GTM playbook for {company_name}.

Read the playbook below and extract exactly {n} specific, actionable tasks the company should complete.

Rules:
- Each task must be a single imperative sentence (start with a verb)
- Max 200 characters each
- Be concrete and specific to {company_name} — no generic advice
- Return ONLY a valid JSON array of strings, e.g. ["Task one.", "Task two."]
- No markdown, no extra text, just the JSON array

Playbook:
{playbook_text[:2000]}"""

    try:
        from google.genai import types as genai_types
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=2048,
            ),
        )
        raw = (response.text or "").strip()
        raw = _clean_json_response(raw)
        tasks = json.loads(raw)
        if isinstance(tasks, list):
            return [str(t).strip() for t in tasks if isinstance(t, str) and len(t.strip()) <= 220][:n]
    except Exception as e:
        log_ai_error("extract_tasks_from_playbook", e, {})
    return []


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

    # Initialize new fields to avoid UnboundLocalError if JSON parsing fails
    snapshot.ai_financial_summary = ""
    snapshot.ai_competitor_analysis = ""

    # Avoid duplicate concurrent generation for the same snapshot.
    playbook_lock_key = f"gtm:ai:playbook:{snapshot.id}:lock"
    if not _acquire_lock(playbook_lock_key):
        return (snapshot.ai_playbook or "").strip()

    # Set status to generating
    snapshot.ai_playbook_status = "generating"
    snapshot.save(update_fields=["ai_playbook_status"])

    try:
        # ---- 1️⃣ Attempt Unified Gemini generation
        client = _get_client()
        if client and not _quota_cooldown_active() and _request_budget_available():
            prompt = _build_prompt(snapshot)
            model_id = "gemini-2.5-flash"
            try:
                response = client.models.generate_content(
                    model=model_id,
                    contents=prompt,
                    config=_PLAYBOOK_CONFIG,
                )
                text = response.text.strip()
                if text:
                    try:
                        text = _clean_json_response(text)
                        parsed = json.loads(text)
                        final_playbook_text = parsed.get("markdown_playbook", "")
                        snapshot.ai_risk_status = parsed.get("risk_status", "Low")
                        snapshot.ai_financial_summary = parsed.get("financial_summary", "")
                        snapshot.ai_competitor_analysis = parsed.get("competitor_analysis", "")

                        # Process resources...
                        topics = parsed.get("learning_topics", [])
                        if topics and getattr(snapshot.session, 'workspace', None):
                            from dashboard.models import Resource, AIResourceRecommendation
                            from django.db.models import Q
                            query = Q()
                            for t in topics:
                                query |= Q(name__icontains=t) | Q(description__icontains=t)
                            
                            matches = Resource.objects.filter(workspace=snapshot.session.workspace).filter(query).distinct()[:3]
                            for r in matches:
                                AIResourceRecommendation.objects.get_or_create(
                                    session=snapshot.session,
                                    resource=r,
                                    defaults={'rationale': f"Recommended learning topic based on AI analysis"}
                                )
                    except Exception as json_err:
                        # 🚨 REPORT RESCUE: If JSON fails, manually extract the playbook content
                        logger.error(f"Failed to parse JSON for {snapshot.company_name}: {json_err}. Rescuing playbook text.")

                        # Try multiple strategies to extract markdown content from malformed JSON
                        # Strategy 1: Look for markdown_playbook field with flexible escaping
                        match = re.search(r'["\']?markdown_playbook["\']?\s*:\s*["\']+(.*?)(?=["\'],\s*["\']|["\'],?\s*\}|$)', text, re.DOTALL | re.IGNORECASE)

                        if match:
                            rescuing = match.group(1)
                            # Aggressive cleanup of escape sequences
                            rescuing = rescuing.replace('\\n', '\n').replace('\\\\', '\\').replace('\\"', '"').replace("\\'", "'")
                            final_playbook_text = rescuing.strip()

                        # Strategy 2: Extract from first markdown header
                        if not final_playbook_text and "# " in text:
                            start_of_md = text.find("# ")
                            if start_of_md != -1:
                                # Get content from first # to end or next major delimiter
                                end_match = re.search(r'["\'],?\s*\}', text[start_of_md:])
                                end_pos = end_match.start() + start_of_md if end_match else len(text)
                                final_playbook_text = text[start_of_md:end_pos].replace('\\n', '\n').replace('\\\\', '\\').strip()
                                # Strip trailing quote if present
                                if final_playbook_text and final_playbook_text[-1] in ('"', "'"):
                                    final_playbook_text = final_playbook_text[:-1]

                        # Strategy 3: If still nothing, use the entire text (might contain some markdown)
                        if not final_playbook_text:
                            final_playbook_text = text.replace('\\n', '\n').replace('\\\\', '\\').strip()

                        # Also extract financial_summary and competitor_analysis from malformed JSON
                        financial_match = re.search(r'["\']?financial_summary["\']?\s*:\s*["\']+(.*?)(?=["\'],\s*["\']|["\'],?\s*\}|$)', text, re.DOTALL | re.IGNORECASE)
                        if financial_match:
                            fin_text = financial_match.group(1).replace('\\n', '\n').replace('\\\\', '\\').replace('\\"', '"').strip()
                            if fin_text and fin_text[-1] in ('"', "'"):
                                fin_text = fin_text[:-1]
                            snapshot.ai_financial_summary = fin_text

                        competitor_match = re.search(r'["\']?competitor_analysis["\']?\s*:\s*["\']+(.*?)(?=["\'],\s*["\']|["\'],?\s*\}|$)', text, re.DOTALL | re.IGNORECASE)
                        if competitor_match:
                            comp_text = competitor_match.group(1).replace('\\n', '\n').replace('\\\\', '\\').replace('\\"', '"').strip()
                            if comp_text and comp_text[-1] in ('"', "'"):
                                comp_text = comp_text[:-1]
                            snapshot.ai_competitor_analysis = comp_text

                        risk_match = re.search(r'["\']?risk_status["\']?\s*:\s*["\']+(.*?)(?=["\']|,)', text, re.IGNORECASE)
                        if risk_match:
                            snapshot.ai_risk_status = risk_match.group(1).strip().strip('"\'')

                    if final_playbook_text:
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
                if _is_quota_error(e):
                    _set_quota_cooldown(_extract_retry_delay_seconds(e))
                log_ai_error(
                    "Playbook generation",
                    e,
                    service="google-genai",
                    model=model_id,
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
        snapshot.ai_playbook_status = "done"
        snapshot.save(update_fields=["ai_playbook", "ai_financial_summary", "ai_competitor_analysis", "ai_risk_status", "ai_playbook_status"])

        return final_playbook_text
    except Exception as outer_exc:
        # Mark failed if any outer exception
        snapshot.ai_playbook_status = "failed"
        snapshot.save(update_fields=["ai_playbook_status"])
        logger.error(f"Playbook generation outer exception: {outer_exc}")
        return ""
    finally:
        _release_lock(playbook_lock_key)


# ================================================================
# DIAGNOSTIC PROMPT GENERATOR
# ================================================================
def _build_diagnostic_prompt(session: AssessmentSession, question: Question, score: int) -> str:
    """Create a structured prompt for AI to generate a question-level diagnostic."""
    
    # Contextual data
    company_name = session.company_name or "a B2B company"
    industry = session.industry or "a general industry"
    stage = session.snapshot.band_stage if getattr(session, 'snapshot', None) and session.snapshot.band_stage else "Unspecified"
    ai_metadata = question.ai_metadata if isinstance(question.ai_metadata, dict) else {}
    risk_if_low    = ai_metadata.get("risk_if_low")    or "N/A"
    quick_win      = ai_metadata.get("quick_win_if_low") or "N/A"

    # Score severity mapping
    if score >= 4:
        severity  = {4: "Strong Foundation", 5: "Exceptional Performance"}.get(score, "Success")
        task_desc = (
            f"explain WHY this is a strategic strength for {company_name} and how it "
            f"provides a competitive advantage at the {stage} stage."
        )
    else:
        severity  = {1: "Critical Failure", 2: "Serious Gap", 3: "Improvement Needed"}.get(score, "Low Priority")
        task_desc = (
            f"explain the immediate risk of this low score in the context of the "
            f"{industry} industry and {stage} stage. Known risk: {risk_if_low}. "
            f"Suggested quick win: {quick_win}."
        )

    prompt = f"""
You are a highly experienced Go-To-Market consultant. Provide a brief, actionable insight for the assessment area below.

Company: {company_name} | Industry: {industry} | GTM Stage: {stage}
Question: {question.text}
Score: {score}/5 ({severity})

Task: {task_desc}

Write one concise paragraph (3–4 sentences max) in clean professional prose.
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
    
    # Generate for all scores in weakest_questions now to ensure AI Insights are always present
    # if response.score > 3:
    #    return "" 
        
    client = _get_client()
    if not client or _quota_cooldown_active() or not _request_budget_available():
        logger.warning("GenAI client unavailable for diagnostic insight.")
        fallback = response.question.diagnostic_note or ""
        if fallback and not response.ai_insight:
            response.ai_insight = fallback
            response.save(update_fields=["ai_insight"])
        return fallback

    lock_key = f"gtm:ai:diagnostic:{response.id}:lock"
    if not _acquire_lock(lock_key):
        return (response.ai_insight or "").strip()

    # Set status to generating
    response.ai_insight_status = "generating"
    response.save(update_fields=["ai_insight_status"])

    try:
        prompt = _build_diagnostic_prompt(response.session, response.question, response.score)
        text = ""
        model_id = "gemini-2.5-flash"
        try:
            # Use unified client to generate content
            ai_response = client.models.generate_content(
                model=model_id,
                contents=prompt,
                config=_FAST_CONFIG,
            )
            text = ai_response.text.strip()
            
            if text:
                # 🌟 Save the insight directly to the Response object
                response.ai_insight = text
                response.ai_insight_status = "done"
                response.save(update_fields=["ai_insight", "ai_insight_status"])
                
                # Log usage if metadata is present
                if hasattr(ai_response, 'usage_metadata'):
                    usage = ai_response.usage_metadata
                    total_tokens = usage.total_token_count
                    if MONITORING_AVAILABLE:
                        AIUsageTracker.log_usage(total_tokens, 'diagnostic')
                
                logger.info(f"✅ Diagnostic insight generated for {response.question.id_code}")
                
        except Exception as e:
            if _is_quota_error(e):
                _set_quota_cooldown(_extract_retry_delay_seconds(e))
                log_ai_error(
                "Diagnostic insight generation",
                e,
                service="google-genai",
                model=model_id,
                prompt=prompt,
                extra={"response_id": response.id},
            )
            
            # Fallback to static diagnostic note if AI fails
            if response.question.diagnostic_note:
                text = response.question.diagnostic_note
                response.ai_insight = text
                response.ai_insight_status = "done"
                response.save(update_fields=["ai_insight", "ai_insight_status"])
                logger.info(f"📝 Using static diagnostic for {response.question.id_code}")
            else:
                response.ai_insight_status = "failed"
                response.save(update_fields=["ai_insight_status"])
            
        return text
    finally:
        _release_lock(lock_key)


def generate_diagnostic_insights_batch(responses: list) -> dict:
    """
    Generates diagnostic AI insights for multiple Response objects in a single API call.
    Returns a dict keyed by response.id → insight text.
    Sets ai_insight_status on each Response and saves to DB.
    """
    if not responses:
        return {}

    client = _get_client()
    if not client or _quota_cooldown_active() or not _request_budget_available():
        logger.warning("GenAI client unavailable for batch diagnostic insight.")
        return {}

    # Build per-response JSON blocks for the prompt
    response_blocks = []
    for resp in responses:
        q = resp.question
        session = resp.session
        company_name = getattr(session, 'company_name', None) or "a B2B company"
        industry = getattr(session, 'industry', None) or "a general industry"
        try:
            stage = session.snapshot.band_stage or "Unspecified"
        except Exception:
            stage = "Unspecified"

        ai_metadata = q.ai_metadata if isinstance(q.ai_metadata, dict) else {}
        risk_if_low = ai_metadata.get("risk_if_low") or "N/A"
        quick_win = ai_metadata.get("quick_win_if_low") or "N/A"

        score_label = {1: "Critical Failure", 2: "Serious Gap", 3: "Improvement Needed",
                       4: "Strong Foundation", 5: "Exceptional Performance"}.get(resp.score, "Scored")

        response_blocks.append(
            f'  "{resp.id}": {{\n'
            f'    "question": "{q.text[:100]}",\n'
            f'    "score": {resp.score}/5 ({score_label}),\n'
            f'    "company": "{company_name}", "industry": "{industry}", "stage": "{stage}",\n'
            f'    "risk_if_low": "{risk_if_low}", "quick_win": "{quick_win}"\n'
            f'  }}'
        )

    blocks_text = ",\n".join(response_blocks)

    prompt = f"""You are a Go-To-Market consultant. For each assessment response below, write ONE concise diagnostic paragraph (2-3 sentences max) explaining why the score matters and what specific action to take next.

Assessment responses:
{{
{blocks_text}
}}

Respond ONLY with a valid JSON object mapping each response ID (as a string key) to its insight paragraph.
Example format:
{{
  "42": "Your ICP definition is unclear, which means you're wasting sales cycles on poor-fit leads. Start by documenting 3-5 firmographic filters.",
  "57": "Your attribution model is strong, giving you clear ROI visibility. Extend it to include longer sales cycles."
}}
Do not include markdown code blocks or extra text. Return only the raw JSON object.""".strip()

    model_id = "gemini-2.5-flash"
    resp_ids = [r.id for r in responses]

    # Mark all as generating
    from .models import Response as ResponseModel
    ResponseModel.objects.filter(id__in=resp_ids).update(ai_insight_status="generating")

    try:
        ai_response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=_FAST_CONFIG,
        )
        raw = (ai_response.text or "").strip()
        cleaned = _clean_json_response(raw)
        parsed = json.loads(cleaned)

        results = {}
        for resp in responses:
            insight = parsed.get(str(resp.id), "").strip()
            if insight:
                resp.ai_insight = insight
                resp.ai_insight_status = "done"
                resp.save(update_fields=["ai_insight", "ai_insight_status"])
                results[resp.id] = insight
            else:
                # Fallback to static note
                fallback = resp.question.diagnostic_note or ""
                if fallback:
                    resp.ai_insight = fallback
                    resp.ai_insight_status = "done"
                    resp.save(update_fields=["ai_insight", "ai_insight_status"])
                    results[resp.id] = fallback
                else:
                    resp.ai_insight_status = "failed"
                    resp.save(update_fields=["ai_insight_status"])

        if hasattr(ai_response, 'usage_metadata') and MONITORING_AVAILABLE:
            AIUsageTracker.log_usage(ai_response.usage_metadata.total_token_count, 'diagnostic_batch')

        logger.info(f"Batch diagnostic insights generated for {len(results)}/{len(responses)} responses.")
        return results

    except Exception as e:
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
        log_ai_error(
            "Batch diagnostic generation",
            e,
            service="google-genai",
            model=model_id,
            prompt=prompt[:500],
            extra={"response_ids": resp_ids},
        )
        # Mark all as failed
        ResponseModel.objects.filter(id__in=resp_ids).update(ai_insight_status="failed")
        return {}


def generate_concise_action_items(playbook_text: str) -> list:
    """
    Extracts and summarizes actionable items from the playbook text using AI.
    Returns a list of concise action strings.
    """
    client = _get_client()
    if not client or _quota_cooldown_active() or not _request_budget_available():
        return []

    model_id = "gemini-2.5-flash"
    prompt = f"""
    Extract the key action items from the following GTM playbook text.
    Summarize each action item into a concise, actionable sentence (max 15 words).
    Return the result as a simple list of strings, one per line.
    Do not use bullet points or numbering in the output.
    
    Playbook Text:
    {playbook_text[:8000]}
    """

    try:
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=_ACTION_CONFIG,
        )
        text = response.text.strip()
        # Split by newlines and filter empty lines
        actions = [line.strip() for line in text.split('\n') if line.strip()]
        
        # Log usage
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            total_tokens = usage.total_token_count
            if MONITORING_AVAILABLE:
                AIUsageTracker.log_usage(total_tokens, 'action_extraction')
                
        return actions
    except Exception as e:
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
        log_ai_error(
            "Action item extraction",
            e,
            service="google-genai",
            model=model_id,
            prompt=prompt,
        )
        return []


def _fallback_rewrite_context_note(note_text: str, mode: str) -> str:
    """Deterministic fallback note cleanup when AI is unavailable."""
    cleaned = re.sub(r"\s+", " ", (note_text or "").strip())
    if not cleaned:
        return ""

    if mode == "summarize":
        short = cleaned[:220]
        if len(cleaned) > 220:
            short = short.rstrip(" ,.;:") + "..."
        return short

    if mode == "specific":
        if "last" not in cleaned.lower():
            cleaned += " (based on what we observed in the last 90 days)."
        return cleaned

    # rewrite
    if not cleaned.endswith("."):
        cleaned += "."
    return cleaned[0].upper() + cleaned[1:]


def rewrite_context_note_with_ai(note_text: str, question_text: str = "", mode: str = "rewrite") -> str:
    """Summarize or rewrite user context notes for clearer assessment inputs."""
    mode = (mode or "rewrite").strip().lower()
    if mode not in {"rewrite", "summarize", "specific"}:
        mode = "rewrite"

    source = (note_text or "").strip()
    if not source:
        return ""

    client = _get_client()
    if not client or _quota_cooldown_active() or not _request_budget_available():
        return _fallback_rewrite_context_note(source, mode)

    model_id = "gemini-2.5-flash"
    mode_instruction = {
        "rewrite": "Rewrite the note in clearer plain English while preserving meaning.",
        "summarize": "Summarize the note into one short clear sentence.",
        "specific": "Rewrite the note to be more specific and actionable, without inventing any numbers.",
    }[mode]

    prompt = f"""
You are helping a business user write a clearer assessment note.

Task:
- {mode_instruction}
- Keep it under 320 characters.
- Do not use bullets or markdown.
- Do not invent facts, numbers, or tools.
- Keep the same intent as the original.

Assessment question:
{question_text or 'N/A'}

User note:
{source}
""".strip()

    try:
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=_FAST_CONFIG,
        )
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            return _fallback_rewrite_context_note(source, mode)
        text = re.sub(r"\s+", " ", text)
        return text[:320].rstrip()
    except Exception as e:
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
        log_ai_error(
            "Context note rewrite",
            e,
            service="google-genai",
            model=model_id,
            prompt=prompt,
        )
        return _fallback_rewrite_context_note(source, mode)

def cleanup_client():
    """Close the cached GenAI client to release resources."""
    global _cached_client
    if _cached_client is not None:
        try:
            _cached_client.api_client.close()
        except Exception:
            pass
        _cached_client = None
