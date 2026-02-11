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
from typing import Dict, Any, Optional, List
from django.conf import settings
from django.shortcuts import get_object_or_404
from .models import AssessmentSession, ResultSnapshot, Response, Question, Category, ActionItem
from .views import _compute_scores, _band_for_score
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
# GEMINI INITIALIZATION
# ================================================================
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logger.warning("Gemini not available for chat assistant")

def _init_gemini_chat():
    """Initialize Gemini for chat interactions"""
    api_key = getattr(settings, "GEMINI_API_KEY", None)
    if not GEMINI_AVAILABLE or not api_key:
        return None
    try:
        genai.configure(api_key=api_key)
        return genai.GenerativeModel("models/gemini-2.5-flash")
    except Exception as e:
        log_ai_error("Gemini chat initialization", e, service="google", model="gemini-2.5-flash")
        return None

# ================================================================
# INTENT DETECTION
# ================================================================
INTENTS = {
    "show_scores": [
        "show.*score", "what.*score", "how.*doing", "my.*results",
        "performance", "dashboard", "overview"
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
        "export", "download", "send", "email", "pdf", "report"
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

# ================================================================
# AI-POWERED GENERAL CHAT
# ================================================================
def handle_general_chat(session: AssessmentSession, context: Dict, message: str) -> str:
    """Use Gemini for general conversational queries with full context awareness"""
    model = _init_gemini_chat()
    
    if not model:
        return "I'm having trouble connecting to my AI brain right now. Try asking about your scores, weak areas, or recommendations!"
    
    # Build comprehensive context including all details
    category_details = "\n".join([
        f"  - {c['name']}: {c['score']}/5.0" 
        for c in context['categories']
    ])
    
    weak_questions_detail = ""
    if context['weak_questions']:
        weak_questions_detail = "\n\nSpecific Low-Scoring Questions:\n" + "\n".join([
            f"  - [{q['id_code']}] {q['text']} (Score: {q['score']}/5, Category: {q['category']})"
            for q in context['weak_questions']
        ])
    
    # Build a rich system prompt with COMPLETE context, including user-provided context notes
    context_notes_text = "\n".join(context.get('context_notes', []))
    system_prompt = f"""You are an expert Go-To-Market consultant directly assisting {context['company_name']} in the {context['industry']} industry.

CRITICAL: You have COMPLETE access to their assessment data. NEVER say you don't know or can't access information. Always answer using the data provided below.

=== COMPLETE ASSESSMENT DATA ===
Company: {context['company_name']}
Industry: {context['industry']}
Overall GTM Score: {context['overall_score']}/100
Maturity Stage: {context['stage']}
Stage Description: {context['headline']}
Assessment Status: {"Completed ✓" if context['is_completed'] else "In Progress"}

Category Scores (out of 5.0):
{category_details}

Weakest Areas (Need Focus):
{', '.join([f"{c['name']} ({c['score']}/5)" for c in context['weakest_categories']])}

Strongest Areas (Competitive Advantages):
{', '.join([f"{c['name']} ({c['score']}/5)" for c in context['strongest_categories']])}
{weak_questions_detail}

Action Items Status:
- Total: {context['action_items']['total']}
- To Do: {context['action_items']['todo']}
- In Progress: {context['action_items']['in_progress']}
- Completed: {context['action_items']['done']}

AI Playbook Available: {"Yes" if context['has_playbook'] else "No"}

=== USER-PROVIDED CONTEXT NOTES ===
{context_notes_text if context_notes_text else 'No extra context provided.'}

=== YOUR INSTRUCTIONS ===
1. ALWAYS answer questions using the specific data above
2. If asked about company name, industry, scores, etc. - provide the EXACT information from above
3. Be conversational but authoritative - you KNOW their business
4. Reference specific scores and categories when relevant
5. Keep responses under 250 words unless more detail is needed
6. Use markdown formatting (bold, bullets, headings)
7. NEVER say "I don't have access" or "I can't see" - you have ALL the data above

=== USER'S QUESTION ===
{message}

=== YOUR RESPONSE ===
Provide a helpful, specific answer using the assessment data above:"""

    try:
        response = model.generate_content(system_prompt)
        
        # Log token usage
        if hasattr(response, 'usage_metadata'):
            usage = response.usage_metadata
            total_tokens = usage.total_token_count
            logger.info(
                f"Chat response | Tokens: {usage.prompt_token_count} input + "
                f"{usage.candidates_token_count} output = {total_tokens} total"
            )
            # Track usage against quotas
            if MONITORING_AVAILABLE:
                AIUsageTracker.log_usage(total_tokens, 'chat')
        
        return response.text.strip()
    except Exception as e:
        log_ai_error(
            "General chat response",
            e,
            service="google",
            model="gemini-2.5-flash",
            prompt=system_prompt,
            extra={"session_id": session.uuid},
        )
        
        # Smart fallback responses based on common questions
        msg_lower = message.lower()
        
        # Company/basic info
        if any(word in msg_lower for word in ["company", "name", "industry", "who"]):
            return f"This assessment is for **{context['company_name']}** in the **{context['industry']}** industry. Your overall GTM score is **{context['overall_score']}/100** at the **{context['stage']}** stage."
        
        # Weakest areas
        if any(word in msg_lower for word in ["weak", "worst", "low", "improve", "focus"]):
            weak_list = "\n".join([f"• **{c['name']}:** {c['score']}/5.0" for c in context['weakest_categories']])
            return f"Your weakest areas that need focus:\n\n{weak_list}\n\nThese are your highest-impact improvement opportunities."
        
        # Strongest areas  
        if any(word in msg_lower for word in ["strong", "best", "good", "well"]):
            strong_list = "\n".join([f"• **{c['name']}:** {c['score']}/5.0" for c in context['strongest_categories']])
            return f"Your strongest areas:\n\n{strong_list}\n\nThese are your competitive advantages!"
        
        # Recommendations
        if any(word in msg_lower for word in ["recommend", "suggest", "should", "what to do", "next step"]):
            return f"Based on your **{context['stage']}** stage:\n\n1. Focus on improving **{context['weakest_categories'][0]['name']}** (scored {context['weakest_categories'][0]['score']}/5)\n2. Set up metrics to track progress\n3. Allocate resources to close critical gaps\n\nView your full playbook for detailed action plans!"
        
        # Roadmap
        if any(word in msg_lower for word in ["roadmap", "plan", "timeline", "days", "month"]):
            return f"**Quick 30-60-90 Day Plan:**\n\n**Days 1-30:** Focus on {context['weakest_categories'][0]['name']}\n**Days 31-60:** Build systems and track metrics\n**Days 61-90:** Optimize and scale\n\n**Goal:** Increase your score from {context['overall_score']} to {min(100, int(context['overall_score']) + 15)} points!"
        
        # Default fallback with actual data
        return f"I'm currently experiencing high demand (AI quota limit). Here's what I can tell you:\n\n**Your GTM Score:** {context['overall_score']}/100\n**Stage:** {context['stage']}\n**Top Priority:** Improve {context['weakest_categories'][0]['name']} (scored {context['weakest_categories'][0]['score']}/5)\n\nTry: 'show scores', 'weakest areas', 'recommendations', or 'roadmap'"

# ================================================================
# MAIN CHAT HANDLER
# ================================================================
def process_chat_message(session_id: str, message: str, user=None) -> Dict[str, Any]:
    """
    Main entry point for processing chat messages
    
    Returns:
        Dict with 'response' (text), 'intent' (detected), and 'success' (bool)
    """
    try:
        # Get session
        session = get_object_or_404(AssessmentSession, uuid=session_id)
        
        # Build context
        context = build_session_context(session)
        
        # Detect intent
        intent = detect_intent(message)
        
        # Route to appropriate handler
        # For specific structured requests, use handlers
        # For everything else, use AI for natural conversation
        handler_map = {
            "show_scores": handle_show_scores,
            "export_report": handle_export,
            "schedule_meeting": handle_schedule_meeting,
        }
        
        if intent in handler_map:
            response_text = handler_map[intent](session, context)
        else:
            # Use AI for all conversational queries (weakest areas, recommendations, roadmap, general chat, etc.)
            response_text = handle_general_chat(session, context, message)
        
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
            "success": False,
            "response": "I encountered an error. Please try again or contact support if the issue persists.",
            "intent": "error",
            "error": str(e)
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
