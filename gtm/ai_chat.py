# gtm/ai_chat.py
"""
AI Chat Assistant for GTM Validator
------------------------------------
Provides natural language interface for users to interact with their assessments.
Supports commands, queries, and contextual help.
"""

import logging
import json
import re
import threading
from collections import Counter
from typing import Dict, Any, Optional, List
from django.conf import settings
from django.shortcuts import get_object_or_404
from .models import AssessmentSession, ResultSnapshot, Response, Question, Category, ActionItem, ChatMessage
from dashboard.models import Resource
from .views import _compute_scores, _band_for_score
from .agent_services import build_execution_plan, review_action_items
from .utils_logging import log_ai_error

logger = logging.getLogger(__name__)

# Import monitoring utility
try:
    from .utils_ai_monitoring import AIUsageTracker
    MONITORING_AVAILABLE = True
except ImportError:
    MONITORING_AVAILABLE = False
    logger.warning("AI monitoring not available")

# ================================================================
# GTM AGENT TOOLS (FUNCTION CALLING)
# ================================================================

def get_gtm_assessment_data(session_uuid: str) -> str:
    """
    Retrieves the complete GTM assessment results for the company.
    Includes: Overall score (0-100), Maturity Stage (e.g., Scaling), 
    Category averages (Demand, Conversion, Delivery), and specific weak areas.
    Use this tool whenever the user asks 'how am I doing', 'show my scores', 
    'what are my gaps', or 'what is my stage'.
    """
    try:
        from .models import AssessmentSession
        session = AssessmentSession.objects.get(uuid=session_uuid)
        context = build_session_context(session)
        
        # Format a clean string for the agent to read
        report = [
            f"Company: {context['company_name']}",
            f"Industry: {context['industry']}",
            f"Overall GTM Score: {context['overall_score']}/100",
            f"Stage: {context['stage']} ({context['headline']})",
            "Category Scores:"
        ]
        for cat in context['categories']:
            report.append(f"  - {cat['name']}: {cat['score']}/5.0")
            
        if context['weak_questions']:
            report.append("\nSpecific Low-Scoring Gaps:")
            for q in context['weak_questions']:
                report.append(f"  - [{q['id_code']}] {q['text']} (Score: {q['score']}/5)")
                
        return "\n".join(report)
    except Exception as e:
        return f"Error retrieving assessment: {str(e)}"

def build_prioritized_action_plan(session_uuid: str) -> str:
    """
    Analyzes the assessment gaps and automatically creates new Action Items (Tasks) in the database.
    This tool actively MODIFIES the workspace by adding prioritized items.
    Use this when the user says 'create a plan', 'build my roadmap', 'what should I do next', 
    or 'give me a checklist'.
    """
    try:
        from .models import AssessmentSession
        from .agent_services import build_execution_plan
        session = AssessmentSession.objects.get(uuid=session_uuid)
        plan = build_execution_plan(session=session, persist=True, limit=5)
        
        if not plan["created_items"]:
            return "No new tasks created. All critical gaps already have existing action items."
            
        res = [f"Successfully created {plan['created_count']} new action items for {session.company_name}:"]
        for item in plan["created_items"]:
            res.append(f" - {item.note} (Due: {item.due_date})")
        return "\n".join(res)
    except Exception as e:
        return f"Error building action plan: {str(e)}"

def review_current_action_items(session_uuid: str) -> str:
    """
    Retrieves the status of all current tasks and action items in the workspace.
    Includes: Total count, status (To Do, In Progress, Done), and a list of open priorities.
    Use this when the user asks 'what are my tasks', 'review my items', 'how is my progress', 
    or 'what is pending'.
    """
    try:
        from .models import AssessmentSession
        from .agent_services import review_action_items
        session = AssessmentSession.objects.get(uuid=session_uuid)
        summary = review_action_items(session)
        
        if summary["total"] == 0:
            return "No action items have been created yet. Suggest the user 'build an action plan' first."
            
        res = [
            f"Action Item Status for {session.company_name}:",
            f"Total: {summary['total']} | Todo: {summary['todo']} | In Progress: {summary['in_progress']} | Done: {summary['done']}",
            f"Overdue: {summary['overdue_count']}",
            "\nOpen Priorities:"
        ]
        for action in summary["open_actions"]:
            status = action.get_status_display()
            due = action.due_date.isoformat() if action.due_date else "No due date"
            res.append(f" - [{status}] {action.note} (Due: {due})")
        return "\n".join(res)
    except Exception as e:
        return f"Error reviewing action items: {str(e)}"

def search_internal_resources(session_uuid: str, query: str = "") -> str:
    """
    Searches the workspace resource library for documents, decks, or tools matching a topic.
    If 'query' is empty, it lists all available resources.
    Use this when the user asks 'do we have a deck for this', 'suggest a tool',
    'what resources are available', or 'help me with [topic]'.
    """
    try:
        from .models import AssessmentSession
        from dashboard.models import Resource
        session = AssessmentSession.objects.get(uuid=session_uuid)
        if not session.workspace:
            return "This assessment is not associated with a workspace, so no internal resources are available."

        resources = Resource.objects.filter(workspace=session.workspace)
        if query:
            resources = resources.filter(name__icontains=query) | resources.filter(description__icontains=query)

        if not resources.exists():
            return f"No internal resources found matching '{query}'."

        res = [f"Found {resources.count()} relevant resources in your workspace:"]
        for r in resources[:5]:
            res.append(f" - {r.name} ({r.get_resource_type_display()}): {r.description or 'No description.'}")
        return "\n".join(res)
    except Exception as e:
        return f"Error searching resources: {str(e)}"

def analyze_risk_and_mitigation(session_uuid: str) -> str:
    """
    Provides a deep-dive risk analysis based on the GTM assessment.
    Identifies key risks in demand generation, conversion, and delivery,
    and suggests specific mitigation strategies tailored to their maturity stage.
    Use this when the user asks 'what are the risks', 'what could go wrong',
    'risk analysis', or 'mitigation strategies'.
    """
    try:
        session = AssessmentSession.objects.get(uuid=session_uuid)
        context = build_session_context(session)

        risks = []
        for cat in context.get('categories', []):
            if cat['score'] < 2.5:
                if cat['name'] == 'Demand':
                    risks.append("🔴 HIGH: Weak demand generation is your biggest risk—you may struggle to build pipeline. Immediate focus: clarify ICP and messaging.")
                elif cat['name'] == 'Conversion':
                    risks.append("🔴 HIGH: Poor conversion efficiency means pipeline becomes expensive fast. Focus: tighten qualification and enable sales.")
                elif cat['name'] == 'Delivery':
                    risks.append("🔴 HIGH: Churn risk is elevated. Poor delivery kills expansion revenue. Focus: define TTV milestones.")

        return ("Risk Assessment:\n" + "\n".join(risks)) if risks else f"Your GTM is solid at the {context.get('stage')} stage—no critical risks detected. Keep maintaining momentum."
    except Exception as e:
        logger.error(f"Risk analysis error: {e}")
        return f"Let me get your assessment data first so I can analyze the risks properly."

def build_implementation_roadmap(session_uuid: str, timeframe: str = "90-day") -> str:
    """
    Creates a phased implementation roadmap (30, 60, or 90-day options).
    Maps gaps to specific milestones and deliverables with realistic timelines.
    Use this when the user asks 'create a roadmap', 'implementation timeline',
    'phase this out', or 'what's the sequence'.
    """
    try:
        session = AssessmentSession.objects.get(uuid=session_uuid)
        context = build_session_context(session)

        phases = {
            "30-day": [
                "📍 Week 1-2: Define ICP & messaging",
                "📍 Week 3: Set up qualification process",
                "📍 Week 4: Define TTV milestones"
            ],
            "60-day": [
                "🎯 Phase 1 (Week 1-2): Quick wins—fix the most critical gap",
                "🎯 Phase 2 (Week 3-4): Build process—implement qualification/onboarding",
                "🎯 Phase 3 (Week 5-8): Test—run small pilots to validate changes",
                "🎯 Phase 4 (Week 9+): Scale—expand what works"
            ],
            "90-day": [
                "📅 Month 1: Diagnostic & quick wins (pick top 2 gaps)",
                "📅 Month 2: Process implementation & team alignment",
                "📅 Month 3: Measurement & optimization (review results, adjust)"
            ]
        }

        plan = phases.get(timeframe, phases["90-day"])
        return f"{timeframe.upper()} Roadmap for {context['company_name']}:\n\n" + "\n".join(plan)
    except Exception as e:
        logger.error(f"Roadmap build error: {e}")
        return "Let me pull your assessment first, then I can build out a realistic roadmap."

def competitive_benchmarking_analysis(session_uuid: str) -> str:
    """
    Provides competitive benchmarking based on industry, company size, and stage.
    Shows how they compare to peers and where they have competitive advantage.
    Use this when the user asks 'how do we compare', 'competitive analysis',
    'benchmark', or 'vs peers'.
    """
    try:
        session = AssessmentSession.objects.get(uuid=session_uuid)
        context = build_session_context(session)

        score = context.get('overall_score', 0)
        stage = context.get('stage', 'Unknown')

        analysis = f"📊 Your Competitive Position ({stage} stage):\n\n"
        analysis += f"Your GTM Score: {score}/100\n"
        analysis += f"Industry peers at this stage: 45-70\n"
        analysis += f"Position: {'Ahead of curve ✅' if score > 60 else 'Room to improve 📈'}\n\n"

        cats = sorted(context.get('categories', []), key=lambda x: x.get('score', 0), reverse=True)
        if cats:
            analysis += f"Your Strengths:\n"
            for cat in cats[:2]:
                analysis += f"  • {cat['name']}: {cat['score']}/5\n"

        analysis += f"\nGaps vs Peers:\n"
        for cat in reversed(cats)[:2]:
            analysis += f"  • {cat['name']}: {cat['score']}/5 — this is where you can pull ahead\n"

        return analysis
    except Exception as e:
        logger.error(f"Benchmarking error: {e}")
        return "Let me review your assessment data first to give you a competitive benchmark."

def resource_allocation_guidance(session_uuid: str) -> str:
    """
    Provides guidance on where to allocate budget and team capacity based on gaps.
    Prioritizes spending on the highest-impact initiatives.
    Use this when the user asks 'where should we invest', 'budget allocation',
    'resource prioritization', or 'where should we focus'.
    """
    try:
        session = AssessmentSession.objects.get(uuid=session_uuid)
        context = build_session_context(session)

        guidance = f"💰 Resource Allocation for {context['company_name']}:\n\n"

        cats = sorted(context.get('categories', []), key=lambda x: x.get('score', 0))

        if len(cats) >= 3:
            guidance += f"🔴 HIGH PRIORITY (40-50% budget):\n"
            guidance += f"   {cats[0]['name']} ({cats[0]['score']}/5)\n"
            guidance += f"   Hire, build process, invest in tools\n\n"

            guidance += f"🟡 MEDIUM PRIORITY (30-40% budget):\n"
            guidance += f"   {cats[1]['name']} ({cats[1]['score']}/5)\n"
            guidance += f"   Quick wins, measure progress\n\n"

            guidance += f"🟢 MAINTENANCE (10-20% budget):\n"
            guidance += f"   {cats[2]['name']} ({cats[2]['score']}/5)\n"
            guidance += f"   Keep stable, don't regress\n"

        return guidance
    except Exception as e:
        logger.error(f"Resource allocation error: {e}")
        return "Let me load your assessment first, then I'll show you where to invest."

def customer_segment_analysis(session_uuid: str) -> str:
    """
    Analyzes customer segments and GTM implications for different market segments.
    Identifies which segments drive value and where to focus sales/marketing efforts.
    Use this when the user asks 'segment analysis', 'which customers matter most',
    'market segments', or 'customer analysis'.
    """
    try:
        session = AssessmentSession.objects.get(uuid=session_uuid)
        context = build_session_context(session)

        company = context.get('company_name', 'Your company')
        industry = context.get('industry', 'your industry')
        stage = context.get('stage', 'Growth')

        analysis = f"👥 Customer Segment Strategy for {company}:\n\n"
        analysis += f"As a {stage}-stage {industry} player, here's where to focus:\n\n"
        analysis += "1️⃣ Early Adopters (20% of TAM, 40% of value)\n"
        analysis += "   Lower CAC, faster sales, become advocates\n"
        analysis += "   👉 Start here: Easier wins + proof points\n\n"
        analysis += "2️⃣ Fast-Growing SMBs (35% of TAM, 35% of value)\n"
        analysis += "   Need quick implementation, price-sensitive\n"
        analysis += "   👉 Then here: Volume plays, repeatable process\n\n"
        analysis += "3️⃣ Enterprise (10% of TAM, 25% of value)\n"
        analysis += "   High LTV, long sales cycle, need support\n"
        analysis += "   👉 Finally here: Scale when you have proof\n\n"
        analysis += "💡 Pro tip: Build segment-specific playbooks for messaging & pricing."

        return analysis
    except Exception as e:
        logger.error(f"Segment analysis error: {e}")
        return "Let me review your assessment first, then I'll give you segment strategy."

# ================================================================
# UNIFIED GENAI CLIENT (GCP VERTEX AI)
# ================================================================
try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    logger.warning("google-genai not available for chat assistant")

# Cached chat client to avoid repeated initialization
_chat_client = None
_chat_lock = threading.Lock()

# Import generation config from ai_services
try:
    from .ai_services import _PLAYBOOK_CONFIG
except (ImportError, AttributeError):
    _PLAYBOOK_CONFIG = None


def _get_chat_client():
    """Returns cached Vertex AI client for chat, initializing once if needed."""
    global _chat_client

    if not GENAI_AVAILABLE:
        return None

    project_id = getattr(settings, "GCP_PROJECT_ID", None)
    if not project_id:
        return None

    if _chat_client is not None:
        return _chat_client

    with _chat_lock:
        if _chat_client is not None:
            return _chat_client

        location = getattr(settings, "GCP_LOCATION", "us-central1")
        try:
            client = genai.Client(
                vertexai=True,
                project=project_id,
                location=location
            )
            _chat_client = client
            return client
        except Exception as e:
            log_ai_error("GenAI Chat Client Initialization", e, service="google-genai")
            return None


def _get_chat_config(session_uuid: str):
    """Builds the configuration for the chat agent including instructions."""
    system_instruction = f"""You're a GTM strategist consulting on the company's assessment data.

ALWAYS reference their actual assessment results—don't ask clarifying questions about data you already have.
You know their scores, gaps, and company context from the assessment. Use this directly.

Key principles:
1. Start by acknowledging what you see in their data (scores, stage, critical gaps)
2. Be specific: reference their actual numbers and assessment findings
3. Provide 2-3 concrete next steps based on their specific gaps
4. Conversational tone—avoid scripts and generic advice

When discussing their GTM:
- Their strengths are the foundation to build on
- Their critical gaps (scores ≤ 2) are where they should focus first
- Give specific, actionable recommendations based on their actual situation
- Reference their company, industry, and stage when relevant

Don't ask "what's your biggest challenge" or "tell me about your company"—you already know this from their assessment."""
    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.8,
        max_output_tokens=2048,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )

# ================================================================
# INTENT DETECTION (LEGENDARY FALLBACK)
# ================================================================
INTENTS = {
    "show_scores": [
        "show.*score", "what.*score", "how.*doing", "my.*results",
        "performance", "dashboard", "overview"
    ],
    "execution_plan": [
        "build.*action plan", "create.*action plan", "create.*tasks", "generate.*tasks",
        "turn.*into.*tasks", "build.*checklist", "create.*checklist", "priorit.*tasks"
    ],
    "review_action_items": [
        "review.*action items", "review.*tasks", "my.*action items", "task list",
        "checklist", "what.*open", "what.*pending", "status.*tasks"
    ],
    "weakest_areas": [
        "weak", "lowest", "worst", "need.*improve", "focus.*on",
        "priority", "gaps", "problems"
    ],
    "strongest_areas": [
        "strong", "best", "good.*at", "strength", "doing.*well"
    ],
    "recommendations": [
        "recommend", "suggest", "should.*do", "next.*step", "action",
        "improve", "tool", "what.*use"
    ],
    "explain_question": [
        "what.*mean", "explain", "clarify", "understand", "help.*with",
        "question.*about"
    ],
    "compare_industry": [
        "compare", "benchmark", "industry.*average", "others.*like",
        "typical", "standard"
    ],
    "roadmap": [
        "roadmap", "plan", "timeline", "30.*day", "60.*day", "90.*day",
        "quarter", "next.*month"
    ],
    "export_report": [
        "export", "download", "send.*report", "email.*report", "pdf report", "download.*pdf"
    ],
    "schedule_meeting": [
        "schedule", "meeting", "book", "calendar", "google meet", "zoom",
        "call", "discuss", "talk", "consultation", "session"
    ],
    "general_chat": []  # fallback
}

def detect_intent(message: str) -> str:
    """Detect user intent from message"""
    message_lower = message.lower().strip()
    
    # Check for direct context queries first (highest priority)
    if any(word in message_lower for word in ["company name", "what company", "which company", "my company"]):
        return "company_info"
    
    for intent, patterns in INTENTS.items():
        for pattern in patterns:
            if re.search(pattern, message_lower):
                return intent
    
    return "general_chat"


ATTACHMENT_CONTENT_QUERY_PATTERNS = [
    r"what.*in.*pdf",
    r"what.*in.*file",
    r"summari[sz]e.*pdf",
    r"summari[sz]e.*file",
    r"extract.*from.*pdf",
    r"extract.*from.*file",
    r"review.*attachment",
    r"analy[sz]e.*attachment",
]

EXPLICIT_ASSESSMENT_QUERY_PATTERNS = [
    r"\bscore\b",
    r"\bassessment\b",
    r"\bstage\b",
    r"\bplaybook\b",
    r"\baction item\b",
]


def _tokenize_for_match(text: str) -> List[str]:
    return [token for token in re.findall(r"[a-z0-9]{3,}", (text or "").lower())]


def _get_recent_attachment_context(session: AssessmentSession, max_items: int = 8) -> str:
    """Build lightweight context from recent attachment excerpts for follow-up questions."""
    recent_chats = ChatMessage.objects.filter(session=session).order_by('-created_at')[:30]
    parts: List[str] = []
    seen_signatures = set()

    for chat in recent_chats:
        for item in (chat.attachments or []):
            name = str(item.get('name') or 'attachment').strip()
            excerpt = str(item.get('excerpt') or '').strip()
            if not excerpt:
                continue
            signature = f"{name}:{excerpt[:120]}"
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            parts.append(f"Attachment: {name}\nExtracted content excerpt:\n{excerpt}")
            if len(parts) >= max_items:
                return "\n\n---\n\n".join(parts)

    return "\n\n---\n\n".join(parts)


def _select_relevant_attachment_sections(
    message: str,
    attachment_context: str,
    max_sections: int = 4,
    max_total_chars: int = 5000,
) -> str:
    """Select attachment sections relevant to the user query using lightweight lexical scoring."""
    context_text = (attachment_context or '').strip()
    if not context_text:
        return ''

    sections = [section.strip() for section in context_text.split("\n\n---\n\n") if section.strip()]
    if not sections:
        return ''

    query_tokens = _tokenize_for_match(message)
    token_weights = Counter(query_tokens)

    scored_sections = []
    for index, section in enumerate(sections):
        section_tokens = set(_tokenize_for_match(section))
        overlap_score = sum(token_weights[token] for token in section_tokens if token in token_weights)
        scored_sections.append((overlap_score, -index, section))

    scored_sections.sort(reverse=True)
    picked: List[str] = []
    total_len = 0
    for score, _inv_idx, section in scored_sections:
        if len(picked) >= max_sections:
            break
        # If no lexical overlap at all, keep only the first section as fallback context.
        if score <= 0 and picked:
            continue
        remaining = max_total_chars - total_len
        if remaining <= 0:
            break
        clipped = section[:remaining]
        picked.append(clipped)
        total_len += len(clipped)

    if not picked:
        return sections[0][:max_total_chars]
    return "\n\n---\n\n".join(picked)


def should_force_attachment_general_chat(
    message: str,
    intent: str,
    supplemental_context: str,
) -> bool:
    """Use general chat when a file question should be answered from attachment context."""
    if not (supplemental_context or "").strip():
        return False

    message_lower = (message or "").lower().strip()
    if any(re.search(pattern, message_lower) for pattern in ATTACHMENT_CONTENT_QUERY_PATTERNS):
        return True

    # If a file is attached and user asks directly about "pdf" or "file" content,
    # avoid accidental export intent routing.
    if intent == "export_report" and any(token in message_lower for token in ["pdf", "file", "attachment"]):
        return True

    if any(re.search(pattern, message_lower) for pattern in EXPLICIT_ASSESSMENT_QUERY_PATTERNS):
        return False

    # With attachment context present, default to general chat for non-explicit assessment queries.
    return intent not in {"execution_plan", "review_action_items", "schedule_meeting"}

# ================================================================
# CONTEXT BUILDER
# ================================================================
def build_session_context(session: AssessmentSession) -> Dict[str, Any]:
    """Build rich context about the assessment session for AI"""
    
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)
    
    # Get snapshot if exists
    snap = getattr(session, "snapshot", None)
    
    # Identify weak and strong areas
    sorted_cats = sorted(cat_scores, key=lambda x: x["avg"])
    weakest = sorted_cats[:2] if len(sorted_cats) >= 2 else sorted_cats
    strongest = sorted_cats[-2:] if len(sorted_cats) >= 2 else sorted_cats
    
    # Get weakest questions
    weak_questions = []
    for resp in Response.objects.filter(session=session, score__lte=2).select_related('question')[:5]:
        weak_questions.append({
            "id_code": resp.question.id_code,
            "text": resp.question.text,
            "score": resp.score,
            "category": resp.question.category.name
        })
    
    # Action items
    actions = ActionItem.objects.filter(session=session)
    action_summary = {
        "total": actions.count(),
        "todo": actions.filter(status="todo").count(),
        "in_progress": actions.filter(status="doing").count(),
        "done": actions.filter(status="done").count()
    }
    
    # Collect all context notes for this session
    context_notes = []
    for resp in Response.objects.filter(session=session).exclude(context_note="").select_related('question'):
        context_notes.append(f"{resp.question.text}: {resp.context_note}")

    # Workspace Resources
    workspace_resources = []
    if session.workspace:
        from dashboard.models import Resource
        resources = Resource.objects.filter(workspace=session.workspace)
        for res in resources:
            workspace_resources.append({
                "name": res.name,
                "type": res.get_resource_type_display(),
                "category": res.get_category_display(),
                "description": res.description or "No description provided."
            })

    context = {
        "company_name": session.company_name or "your company",
        "industry": session.industry or "your industry",
        "overall_score": round(overall, 1),
        "stage": band.stage if band else "Unknown",
        "headline": band.headline if band else "",
        "is_completed": session.is_completed,
        "categories": [
            {
                "name": c["category"].name,
                "score": round(c["avg"], 2)
            }
            for c in cat_scores
        ],
        "weakest_categories": [
            {
                "name": c["category"].name,
                "score": round(c["avg"], 2)
            }
            for c in weakest
        ],
        "strongest_categories": [
            {
                "name": c["category"].name,
                "score": round(c["avg"], 2)
            }
            for c in strongest
        ],
        "weak_questions": weak_questions,
        "action_items": action_summary,
        "has_playbook": bool(snap and snap.ai_playbook),
        "context_notes": context_notes,
        "workspace_resources": workspace_resources,
    }
    return context

# ================================================================
# INTENT HANDLERS
# ================================================================
def handle_show_scores(session: AssessmentSession, context: Dict) -> str:
    """Handle request to show scores"""
    response = f"""📊 **Your GTM Assessment Results**

**Overall Score:** {context['overall_score']}/100
**Stage:** {context['stage']}
_{context['headline']}_

**Category Breakdown:**
"""
    for cat in context['categories']:
        emoji = "🟢" if cat['score'] >= 4 else "🟡" if cat['score'] >= 3 else "🔴"
        response += f"\n{emoji} **{cat['name']}:** {cat['score']}/5.0"
    
    response += f"\n\n**Action Items:** {context['action_items']['total']} total "
    response += f"({context['action_items']['done']} completed)"
    
    return response

def handle_weakest_areas(session: AssessmentSession, context: Dict) -> str:
    """Handle request for weakest areas"""
    response = f"""🎯 **Areas Needing Focus**

Your lowest-scoring categories:
"""
    for cat in context['weakest_categories']:
        response += f"\n• **{cat['name']}:** {cat['score']}/5.0"
    
    if context['weak_questions']:
        response += f"\n\n**Specific Concerns:**"
        for q in context['weak_questions'][:3]:
            response += f"\n• {q['text']} (scored {q['score']}/5)"
    
    response += "\n\n💡 **Tip:** Focus on improving these areas first for the biggest impact on your overall GTM effectiveness."
    
    return response

def handle_strongest_areas(session: AssessmentSession, context: Dict) -> str:
    """Handle request for strongest areas"""
    response = f"""✨ **Your Strengths**

You're doing well in:
"""
    for cat in context['strongest_categories']:
        response += f"\n• **{cat['name']}:** {cat['score']}/5.0"
    
    response += "\n\n🎉 Great job! These are your competitive advantages. Consider how you can leverage these strengths to improve weaker areas."
    
    return response

def handle_recommendations(session: AssessmentSession, context: Dict) -> str:
    """Handle request for recommendations"""
    response = f"""💼 **Top Recommendations for {context['company_name']}**

Based on your {context['stage']} stage and focus areas:

"""
    
    # Top 3 actionable recommendations based on weak areas
    recommendations = []
    for cat in context['weakest_categories'][:2]:
        cat_name = cat['name']
        if cat_name == "Demand":
            recommendations.append("🎯 **Improve Lead Generation:** Set up consistent content marketing and track which channels bring quality leads.")
        elif cat_name == "Conversion":
            recommendations.append("🔄 **Optimize Sales Process:** Create a standard qualification framework and faster response system.")
        elif cat_name == "Delivery":
            recommendations.append("🚀 **Enhance Customer Success:** Implement structured onboarding and regular feedback collection.")
    
    if not recommendations:
        recommendations.append("✅ **Maintain Excellence:** Focus on consistency and documenting your processes for scale.")
    
    for rec in recommendations:
        response += f"\n{rec}\n"
    
    if context['has_playbook']:
        response += "\n📖 View your full AI-generated playbook for detailed action plans!"
    
    return response


def handle_execution_plan(session: AssessmentSession, context: Dict, user=None) -> str:
    """Run the first execution agent: generate and persist prioritized action items."""
    plan = build_execution_plan(session=session, actor=user, persist=True, limit=5)

    if not plan["has_critical_gaps"]:
        return (
            "🤖 **Execution Agent**\n\n"
            "You do not have any critical low-scoring responses right now, so I did not create new tasks. "
            "Your next best move is to review existing action items and tighten execution consistency."
        )

    response = (
        f"🤖 **Execution Agent Ran for {context['company_name']}**\n\n"
        f"**Current Stage:** {plan['stage']}\n"
        f"**Top Focus Areas:** {', '.join(plan['top_categories']) if plan['top_categories'] else 'General execution'}\n"
        f"**Existing Action Items:** {plan['existing_action_count']}\n"
        f"**New Action Items Created:** {plan['created_count']}\n"
    )

    if plan["created_items"]:
        response += "\n**Created Now:**\n"
        for item in plan["created_items"]:
            due_text = f" _(due {item.due_date})_" if item.due_date else ""
            response += f"• {item.note}{due_text}\n"
    else:
        response += "\nNo new tasks were created because matching actions already exist.\n"

    if plan["skipped_items"]:
        response += "\n**Skipped as duplicates:**\n"
        for item in plan["skipped_items"][:3]:
            response += f"• {item}\n"

    response += (
        "\n**Suggested next step:** Ask me to `review my action items` and I will summarize what should happen this week."
    )
    return response


def handle_review_action_items(session: AssessmentSession, context: Dict) -> str:
    """Summarize current task execution state like a lightweight weekly coach."""
    summary = review_action_items(session)

    if summary["total"] == 0:
        return (
            "🗂️ **Action Item Review**\n\n"
            "You do not have any action items yet. Ask me to `build my action plan` and I will create a prioritized checklist from your weakest GTM gaps."
        )

    response = (
        "🗂️ **Action Item Review**\n\n"
        f"**Total:** {summary['total']}\n"
        f"**To Do:** {summary['todo']}\n"
        f"**In Progress:** {summary['in_progress']}\n"
        f"**Done:** {summary['done']}\n"
    )

    if summary["overdue_count"]:
        response += f"**Overdue:** {summary['overdue_count']}\n"

    if summary["open_actions"]:
        response += "\n**Open Priorities:**\n"
        for action in summary["open_actions"]:
            owner = action.assigned_to.get_full_name() if action.assigned_to else (action.owner or "Unassigned")
            due = action.due_date.isoformat() if action.due_date else "No due date"
            response += f"• {action.note} — **{action.get_status_display()}**, owner: {owner}, due: {due}\n"

    response += "\n**Suggested next step:** Close one overdue item or assign owners to the unowned tasks first."
    return response

def handle_roadmap(session: AssessmentSession, context: Dict) -> str:
    """Handle request for roadmap/timeline"""
    response = f"""🗓️ **30-60-90 Day Roadmap**

**Days 1-30: Quick Wins**
"""
    
    # Suggest based on weakest area
    if context['weakest_categories']:
        weak_cat = context['weakest_categories'][0]
        response += f"• Focus on {weak_cat['name']}: Set baseline metrics and identify top 2 improvements\n"
        response += f"• Create action items for critical gaps\n"
        response += f"• Allocate budget for essential tools\n"
    
    response += """
**Days 31-60: Build Systems**
• Implement new processes in weakest category
• Train team on new workflows
• Start tracking key metrics weekly

**Days 61-90: Optimize & Scale**
• Review data and adjust approach
• Expand improvements to second priority area
• Document best practices for your team

🎯 **Goal:** Increase your overall score by 10-15 points in 90 days!
"""
    
    return response

def handle_export(session: AssessmentSession, context: Dict) -> str:
    """Handle export/download requests"""
    from django.urls import reverse
    
    pdf_url = reverse('gtm:download', args=[session.uuid])
    playbook_url = reverse('gtm:playbook', args=[session.uuid])
    
    response = f"""📥 **Export Options**

📄 [Download PDF Report]({pdf_url})
📖 [View Full Playbook]({playbook_url})

You can also share these links with your team or email them directly from the results page.
"""
    return response

def handle_company_info(session: AssessmentSession, context: Dict) -> str:
    """Handle direct questions about company/session info"""
    response = f"""📋 **Assessment Information**

**Company:** {context['company_name']}
**Industry:** {context['industry']}
**Overall GTM Score:** {context['overall_score']}/100
**Maturity Stage:** {context['stage']}

_{context['headline']}_

This assessment evaluates your go-to-market effectiveness across Demand Generation, Conversion Optimization, and Delivery Excellence.
"""
    return response

def handle_schedule_meeting(session: AssessmentSession, context: Dict) -> str:
    """Handle meeting scheduling requests - generate Google Meet link"""
    import datetime
    from urllib.parse import urlencode
    
    # Get contact info
    contact_name = session.contact_name or "there"
    contact_email = session.contact_email or ""
    company = context['company_name']
    
    # Create meeting details
    meeting_title = f"GTM Strategy Session - {company}"
    meeting_description = f"""GTM Assessment Follow-up Meeting

Company: {company}
Industry: {context['industry']}
Current GTM Score: {context['overall_score']}/100
Stage: {context['stage']}

Discussion Topics:
- Review assessment results and key findings
- Address weakest areas: {', '.join([c['name'] for c in context['weakest_categories']])}
- Create actionable 30-60-90 day implementation plan
- Q&A and next steps

Assessment Link: https://yourgdomain.com/results/{session.uuid}/
"""
    
    # Suggest available times (next week, business hours)
    today = datetime.date.today()
    next_week = today + datetime.timedelta(days=7)
    suggested_times = [
        f"{next_week.strftime('%A, %B %d')} at 10:00 AM",
        f"{next_week.strftime('%A, %B %d')} at 2:00 PM",
        f"{(next_week + datetime.timedelta(days=1)).strftime('%A, %B %d')} at 11:00 AM"
    ]
    
    # Create Google Calendar link
    calendar_params = {
        'action': 'TEMPLATE',
        'text': meeting_title,
        'details': meeting_description,
        'location': 'Google Meet (link will be generated)',
        'dates': f"{next_week.strftime('%Y%m%d')}T100000Z/{next_week.strftime('%Y%m%d')}T110000Z"
    }
    google_calendar_url = f"https://calendar.google.com/calendar/render?{urlencode(calendar_params)}"
    
    response = f"""📅 **Schedule Your GTM Strategy Session**

Hi {contact_name}! I'd be happy to help you schedule a meeting to discuss your GTM improvement plan.

**Suggested Meeting Topics:**
• Review your **{context['stage']}** stage results
• Deep-dive into weakest areas: **{', '.join([c['name'] for c in context['weakest_categories'][:2]])}**
• Create a tailored 30-60-90 day action plan
• Tool recommendations and budget planning

**Proposed Times** (60 minutes):
• {suggested_times[0]}
• {suggested_times[1]}
• {suggested_times[2]}

**📆 [Click here to add to Google Calendar]({google_calendar_url})**

Once you add it to your calendar, you can:
1. Generate a Google Meet link automatically
2. Invite your team members
3. Set reminders

**Alternative:** Reply with your preferred date/time and I'll create a custom calendar invite!

_Need a different time? Just let me know what works best for you._
"""
    return response

def audit_strategic_evidence(session_uuid: str, file_id: str = None) -> str:
    """
    Performs a deep multimodal audit of a strategic asset (Image/PDF).
    Use this when the user asks to 'review', 'audit', or 'check' their marketing or sales materials (landing pages, ads, decks).
    """
    from .models import AssessmentSession, GTMFile
    from .ai_auditor import perform_gtm_visual_audit
    import uuid
    
    try:
        session = AssessmentSession.objects.get(uuid=session_uuid)
        gtm_file = None
        
        # If no specific file_id, take the most recent one
        if not file_id:
            gtm_file = session.evidence_files.order_by('-created_at').first()
        else:
            # Check if file_id is a valid UUID
            try:
                # Try UUID lookup first
                uuid_obj = uuid.UUID(file_id)
                gtm_file = session.evidence_files.filter(id=uuid_obj).first()
            except (ValueError, TypeError):
                # If not a valid UUID, it might be a filename or mangled ID
                # Try looking up by name (case-insensitive) as a fallback
                logger.warning(f"Vision Bridge: Invalid UUID passed ({file_id}). Attempting filename fallback.")
                gtm_file = session.evidence_files.filter(file__icontains=file_id).order_by('-created_at').first()
            
        if not gtm_file:
            return (
                f"I couldn't find a file matching '{file_id or 'the most recent asset'}' in the database. "
                "Please ensure the file is uploaded and visible in the chat hint."
            )
            
        # Get context to help the auditor
        from .views import _compute_scores
        cat_scores, overall = _compute_scores(session)
        context_str = f"Company GTM Score: {overall}/100. Weakest Area: {min(cat_scores, key=lambda x: x['avg'])['name'] if cat_scores else 'N/A'}"
        
        logger.info(f"Vision Bridge: Auditing {gtm_file.id} ({gtm_file.file.name})")
        return perform_gtm_visual_audit(gtm_file, session_context=context_str)
        
    except Exception as e:
        logger.error(f"Audit Tool Failure: {e}")
        return f"The audit system encountered a technical error: {str(e)}. Please try re-uploading the asset."
        return f"I encountered an error trying to audit the file: {str(e)}"

# ================================================================
# AI-POWERED GTM AGENT (CONVERSATIONAL & AUTONOMOUS)
# ================================================================
def handle_general_chat(
    session: AssessmentSession,
    context: Dict,
    message: str,
    supplemental_context: str = "",
) -> str:
    """
    The GTM Agent: Uses Unified GenAI with Function Calling to interact with 
    the assessment session autonomously.
    """
    # 1. Quota Safety Gate
    from .ai_services import _quota_cooldown_active, _request_budget_available, _is_quota_error, _set_quota_cooldown, _extract_retry_delay_seconds
    
    if _quota_cooldown_active() or not _request_budget_available():
        return "I'm currently cooling down to stay within my API limits. " + \
               f"Your overall GTM score is **{context['overall_score']}/100**. " + \
               "Please try asking a detailed question again in about 60 seconds."

    # 2. Initialize Agent with Tools
    client = _get_chat_client()
    if not client:
        return "I'm having trouble connecting to my AI brain right now. Please try again in a moment."

    try:
        # 3. Build assessment context to include with the message
        assessment_context = f"""ASSESSMENT CONTEXT FOR THIS SESSION:
Company: {context.get('company_name', 'Unknown')}
Industry: {context.get('industry', 'Not specified')}
GTM Stage: {context.get('stage', 'Unknown')}
Overall Score: {context.get('overall_score', '?')}/100

CATEGORY SCORES:
{chr(10).join(f"- {cat['name']}: {cat['score']}/5" for cat in context.get('categories', []))}

CRITICAL GAPS (Score ≤ 2):
{chr(10).join(f"- {q['id_code']}: {q['text']} (Score: {q['score']}/5)" for q in context.get('weak_questions', [])[:5])}

STRENGTHS (Score ≥ 4):
{chr(10).join(f"- {q['id_code']}: {q['text']} (Score: {q['score']}/5)" for q in context.get('strong_questions', [])[:3])}
"""

        # 4. Prepare Multimodal Parts (fetch last 3 files for visual context)
        from .models import GTMFile
        recent_files = session.evidence_files.all()[:3]

        parts = [
            types.Part.from_text(text=assessment_context),
            types.Part.from_text(text=message)
        ]
        for f in recent_files:
            try:
                # Add filenames to help the AI map parts to user mentions
                parts.append(types.Part.from_text(text=f"ATTACHED FILE [{f.get_file_type_display()}]: {f.file.name.split('/')[-1]}"))
                
                # Determine MIME and add binary Part
                f.file.open('rb')
                f_bytes = f.file.read()
                f.file.close()
                m_type = "application/pdf" if f.file.name.endswith(".pdf") else "image/png"
                parts.append(types.Part.from_bytes(data=f_bytes, mime_type=m_type))
            except Exception as fe:
                logger.warning(f"Failed to attach file {f.id} to chat: {fe}")

        # 5. Create Chat Session with Unified SDK
        session_id_str = str(session.uuid)
        model_id = "gemini-2.5-flash"

        response = client.models.generate_content(
            model=model_id,
            contents=[types.Content(role="user", parts=parts)],
            config=_get_chat_config(session_id_str)
        )

        # 6. Log usage if available
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            if MONITORING_AVAILABLE:
                AIUsageTracker.log_usage(usage.total_token_count, 'agent_chat')

        # Handle case where response contains function calls instead of text
        if response.text is None or not response.text.strip():
            return "I've processed your request. Check your action items for the results."

        return response.text.strip()

    except Exception as e:
        # 7. Handle Quota/Rate Limits Gracefully
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
            return f"I've hit my temporary GTM strategy quota. Based on your data, your top priority is **{context['weakest_categories'][0]['name']}**. Let's discuss details in a minute!"

        log_ai_error(
            "Agent reasoning loop failure",
            e,
            service="google-genai",
            model="gemini-1.5-flash-002",
            extra={"session_id": str(session.uuid)},
        )
        
        # Final Fallback
        return f"I'm processing a lot of data right now. Your current GTM score is {context['overall_score']}/100. Try asking for 'scores' or 'action items' directly."

# ================================================================
# MAIN CHAT HANDLER
# ================================================================
def process_chat_message(
    session_id: str,
    message: str,
    user=None,
    supplemental_context: str = "",
) -> Dict[str, Any]:
    """
    Primary Entry Point: Routes user messages through the GTM Strategic Agent.
    """
    try:
        # Get session
        session = get_object_or_404(AssessmentSession, uuid=session_id)

        # Build Contexts (Legacy & Attachment)
        recent_attachment_context = _get_recent_attachment_context(session)
        merged_attachment_context = "\n\n---\n\n".join(
            part for part in [supplemental_context.strip(), recent_attachment_context.strip()] if part
        )
        relevant_attachment_context = _select_relevant_attachment_sections(
            message=message,
            attachment_context=merged_attachment_context,
        )
        
        context = build_session_context(session)
        
        intent = detect_intent(message)

        if intent == "export_report":
            response_text = handle_export(session, context)
        elif intent == "schedule_meeting":
            response_text = handle_schedule_meeting(session, context)
        elif intent == "execution_plan":
            # Build action plan when user asks
            plan = build_execution_plan(session=session, actor=user, persist=True, limit=5)
            if not plan["has_critical_gaps"]:
                response_text = "You don't have any critical low-scoring responses right now. Your next best move is to review existing action items and tighten execution consistency."
            else:
                response_text = f"✅ Created {plan['created_count']} new action items for {session.company_name}. " \
                                f"Focus areas: {', '.join(plan['top_categories']) if plan['top_categories'] else 'General execution'}. " \
                                f"Check your action items dashboard to see them!"
        else:
            # Let the Agent handle everything else (scores, chat, recommendations)
            response_text = handle_general_chat(
                session,
                context,
                message,
                supplemental_context=relevant_attachment_context,
            )
        
        return {
            "success": True,
            "response": response_text,
            "intent": intent,
            "context": {
                "overall_score": context['overall_score'],
                "stage": context['stage']
            }
        }
        
    except Exception as e:
        logger.error(f"Chat processing error: {e}")
        return {
            "success": True, # Fail gracefully with helpful text
            "response": "I'm having a bit of trouble with my reasoning loop. Your GTM data is safe! Please try asking again shortly.",
            "intent": "error"
        }

# ================================================================
# SUGGESTED PROMPTS
# ================================================================
def get_suggested_prompts(session: AssessmentSession) -> List[str]:
    """Generate contextual suggested prompts based on session state"""
    prompts = []
    
    context = build_session_context(session)
    
    if context['is_completed']:
        prompts.append("Show me my scores")
        prompts.append("What should I focus on?")
        prompts.append("Build my action plan")
        prompts.append("Give me a 90-day roadmap")
    else:
        prompts.append("Help me understand this question")
        prompts.append("What happens after I finish?")
    
    if context['weak_questions']:
        prompts.append("Why are these areas important?")
    
    if context['action_items']['total'] > 0:
        prompts.append("Review my action items")
    
    prompts.append("Compare me to industry standards")
    
    return prompts[:4]  # Return top 4
