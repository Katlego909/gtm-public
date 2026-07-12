"""Chat/intent layer for the three workspace-scoped agents: portfolio,
resource, and insights. Mirrors gtm/ai_chat.py's regex-intent-router +
single-shot-Gemini-fallback pattern, but scoped to a Workspace (one client
company) instead of a single AssessmentSession.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from .ai_chat import GENAI_AVAILABLE, _get_chat_client
from .models_workspace import Workspace
from .utils_logging import log_ai_error
from .workspace_agent_services import (
    build_portfolio_trends,
    build_resource_gap_matches,
    build_workspace_insights_digest,
)

logger = logging.getLogger(__name__)

try:
    from google.genai import types
except ImportError:
    types = None

AGENT_TYPES = ("portfolio", "resource", "insights")


# ================================================================
# CONTEXT BUILDERS
# ================================================================

def build_portfolio_context(workspace: Workspace) -> Dict[str, Any]:
    return {"workspace_name": workspace.name, **build_portfolio_trends(workspace)}


def build_resource_context(workspace: Workspace) -> Dict[str, Any]:
    return {"workspace_name": workspace.name, **build_resource_gap_matches(workspace)}


def build_insights_context(workspace: Workspace) -> Dict[str, Any]:
    return {"workspace_name": workspace.name, **build_workspace_insights_digest(workspace)}


CONTEXT_BUILDERS = {
    "portfolio": build_portfolio_context,
    "resource": build_resource_context,
    "insights": build_insights_context,
}


def build_workspace_agent_context(agent_type: str, workspace: Workspace) -> Dict[str, Any]:
    builder = CONTEXT_BUILDERS.get(agent_type)
    if not builder:
        raise ValueError(f"Unknown agent_type: {agent_type}")
    return builder(workspace)


# ================================================================
# INTENT TABLES (representative, not exhaustive — falls through to
# general_chat/Gemini for anything unmatched)
# ================================================================

PORTFOLIO_INTENTS = {
    "trend_overview": [r"trend", r"over time", r"progress since", r"improv(ed|ing)", r"declin"],
    "category_trend": [r"(demand|conversion|delivery).*(trend|improv|declin)", r"has.*improved on"],
    "session_comparison": [r"compare.*(session|assessment)", r"vs last assessment", r"since last assessment"],
    "portfolio_summary": [r"all assessments", r"every assessment", r"assessment history"],
    "general_chat": [],
}

RESOURCE_INTENTS = {
    "find_resource": [r"do we have", r"is there a (deck|doc|tool|template)", r"resource for", r"playbook for"],
    "recommend_for_gap": [r"what should we use for", r"tool for our weak", r"recommend.*tool"],
    "list_resources": [r"what resources", r"show.*resources", r"resource library"],
    "coverage_gap": [r"missing resources", r"gap in our resources", r"no.*playbook"],
    "general_chat": [],
}

INSIGHTS_INTENTS = {
    "score_change": [r"score drop", r"score improv", r"what changed", r"why did.*score"],
    "activity_summary": [r"what happened", r"recent activity", r"this week", r"digest", r"summary"],
    "pending_review": [r"pending suggestions", r"awaiting review", r"need my approval"],
    "overdue_tasks": [r"overdue", r"stale tasks", r"behind schedule"],
    "general_chat": [],
}

INTENT_TABLES = {
    "portfolio": PORTFOLIO_INTENTS,
    "resource": RESOURCE_INTENTS,
    "insights": INSIGHTS_INTENTS,
}


def detect_intent(agent_type: str, message: str) -> str:
    """Detect intent for a workspace agent message using its intent table."""
    table = INTENT_TABLES.get(agent_type, {})
    message_lower = (message or "").lower().strip()
    for intent, patterns in table.items():
        for pattern in patterns:
            if re.search(pattern, message_lower):
                return intent
    return "general_chat"


# ================================================================
# DETERMINISTIC HANDLERS
# ================================================================

def handle_trend_overview(context: Dict[str, Any], message: str) -> str:
    if context["session_count"] < 2:
        return (
            f"**{context['workspace_name']}** only has {context['session_count']} completed assessment(s) so far — "
            "trends need at least two to compare. Once a follow-up assessment is completed, I can show you how scores moved."
        )
    delta = context["overall_delta"]
    direction = "improved" if delta and delta > 0 else "declined" if delta and delta < 0 else "held steady"
    response = f"**Portfolio Trend for {context['workspace_name']}**\n\n"
    response += f"Across {context['session_count']} assessments, the overall score has {direction}"
    if delta is not None:
        response += f" ({delta:+.1f} points)"
    response += ".\n\n**Category movement:**\n"
    for row in context["category_deltas"]:
        response += f"\n• **{row['category']}:** {row['first']} -> {row['latest']} ({row['delta']:+.2f})"
    return response


def handle_category_trend(context: Dict[str, Any], message: str) -> str:
    message_lower = message.lower()
    matches = [row for row in context["category_deltas"] if row["category"].lower() in message_lower]
    if not matches:
        return handle_trend_overview(context, message)
    row = matches[0]
    direction = "improved" if row["delta"] > 0 else "declined" if row["delta"] < 0 else "stayed flat"
    return (
        f"**{row['category']}** has {direction} for {context['workspace_name']}: "
        f"{row['first']} -> {row['latest']} ({row['delta']:+.2f}) across {context['session_count']} assessments."
    )


def handle_session_comparison(context: Dict[str, Any], message: str) -> str:
    if context["session_count"] < 2:
        return handle_trend_overview(context, message)
    first, latest = context["sessions"][0], context["sessions"][-1]
    return (
        f"**Latest vs First Assessment — {context['workspace_name']}**\n\n"
        f"First ({first['created_at'].strftime('%b %d, %Y')}): {first['overall']}/100, stage {first['band_stage'] or 'Unknown'}\n"
        f"Latest ({latest['created_at'].strftime('%b %d, %Y')}): {latest['overall']}/100, stage {latest['band_stage'] or 'Unknown'}\n\n"
        f"Overall change: {context['overall_delta']:+.1f} points."
    )


def handle_portfolio_summary(context: Dict[str, Any], message: str) -> str:
    if not context["sessions"]:
        return f"**{context['workspace_name']}** doesn't have any completed assessments yet."
    response = f"**Assessment History — {context['workspace_name']}**\n"
    for row in context["sessions"]:
        response += f"\n• {row['created_at'].strftime('%b %d, %Y')}: {row['overall']}/100 ({row['band_stage'] or 'Unknown'})"
    return response


def handle_find_resource(context: Dict[str, Any], message: str) -> str:
    message_lower = message.lower()
    tokens = [t for t in re.findall(r"[a-z0-9]{4,}", message_lower)]
    matches = [
        r for r in context["resources"]
        if r.name.lower() in message_lower or any(token in r.name.lower() for token in tokens)
    ]
    if matches:
        response = "**Found in your resource library:**\n"
        for r in matches[:5]:
            response += f"\n• **{r.name}** ({r.get_category_display()})"
        return response
    return handle_list_resources(context, message)


def handle_recommend_for_gap(context: Dict[str, Any], message: str) -> str:
    if not context["weakest_categories"]:
        return f"No completed assessments yet for **{context['workspace_name']}**, so I don't have gap data to match resources against."
    response = f"**Recommended focus for {context['workspace_name']}'s weakest areas:**\n"
    for category in context["weakest_categories"]:
        response += f"\n• **{category}**"
    if context["unrecommended_resources"]:
        response += "\n\n**Unused resources in your library that may help:**\n"
        for r in context["unrecommended_resources"][:5]:
            response += f"\n• {r.name} ({r.get_category_display()})"
    else:
        response += "\n\nAll your library resources have already been matched to an assessment."
    return response


def handle_list_resources(context: Dict[str, Any], message: str) -> str:
    if not context["resources"]:
        return f"**{context['workspace_name']}** doesn't have any resources in its library yet."
    response = f"**Resource Library — {context['workspace_name']}** ({context['resource_count']} items)\n"
    for r in context["resources"][:10]:
        response += f"\n• **{r.name}** — {r.get_category_display()}"
    return response


def handle_coverage_gap(context: Dict[str, Any], message: str) -> str:
    if not context["coverage_gaps"]:
        return f"**{context['workspace_name']}**'s weakest GTM categories all have at least one matching resource in the library."
    response = f"**Resource coverage gaps — {context['workspace_name']}**\n\nNo library resource matches these weak categories:\n"
    for category in context["coverage_gaps"]:
        response += f"\n• {category}"
    return response


def handle_score_change(context: Dict[str, Any], message: str) -> str:
    trends = {**context["trends"], "workspace_name": context["workspace_name"]}
    return handle_trend_overview(trends, message)


def handle_activity_summary(context: Dict[str, Any], message: str) -> str:
    if not context["recent_events"]:
        return f"No recent workspace activity for **{context['workspace_name']}**."
    response = f"**Recent Activity — {context['workspace_name']}**\n"
    for event in context["recent_events"][:10]:
        actor = (event.actor.get_full_name() or event.actor.username) if event.actor else "Someone"
        response += f"\n• {actor} — {event.summary or event.get_event_type_display()} ({event.created_at.strftime('%b %d')})"
    return response


def handle_pending_review(context: Dict[str, Any], message: str) -> str:
    if not context["pending_suggestions"]:
        return f"No AI suggestions awaiting review for **{context['workspace_name']}**."
    response = f"**{context['pending_suggestion_count']} suggestion(s) awaiting review — {context['workspace_name']}**\n"
    for s in context["pending_suggestions"][:5]:
        response += f"\n• **{s.metric}** ({s.category}): {s.recommendation[:100]}"
    return response


def handle_overdue_tasks(context: Dict[str, Any], message: str) -> str:
    if not context["overdue_actions"]:
        return f"No overdue tasks for **{context['workspace_name']}** — {context['open_action_count']} open task(s) total, all on track."
    response = f"**{context['overdue_action_count']} overdue task(s) — {context['workspace_name']}**\n"
    for action in context["overdue_actions"][:10]:
        assignee = (action.assigned_to.get_full_name() or action.assigned_to.username) if action.assigned_to else "Unassigned"
        response += f"\n• {action.note} (due {action.due_date}, {assignee})"
    return response


def format_insights_digest_text(context: Dict[str, Any]) -> str:
    """Combine the insights context into a single digest message, reusing
    the same section-rendering as the individual chat intent handlers."""
    sections = [
        handle_score_change(context, ""),
        handle_activity_summary(context, ""),
        handle_pending_review(context, ""),
        handle_overdue_tasks(context, ""),
    ]
    return "\n\n---\n\n".join(sections)


DETERMINISTIC_HANDLERS = {
    "portfolio": {
        "trend_overview": handle_trend_overview,
        "category_trend": handle_category_trend,
        "session_comparison": handle_session_comparison,
        "portfolio_summary": handle_portfolio_summary,
    },
    "resource": {
        "find_resource": handle_find_resource,
        "recommend_for_gap": handle_recommend_for_gap,
        "list_resources": handle_list_resources,
        "coverage_gap": handle_coverage_gap,
    },
    "insights": {
        "score_change": handle_score_change,
        "activity_summary": handle_activity_summary,
        "pending_review": handle_pending_review,
        "overdue_tasks": handle_overdue_tasks,
    },
}


# ================================================================
# GEMINI FALLBACK (single-shot, no multi-turn history — mirrors
# ai_chat.handle_general_chat, reuses its cached Vertex AI client)
# ================================================================

SYSTEM_INSTRUCTIONS = {
    "portfolio": (
        "You are a GTM portfolio analyst reviewing one client's full assessment history over time. "
        "Focus on trajectory, not a single snapshot. Be conversational, direct, and concise."
    ),
    "resource": (
        "You are a resource curator surfacing the workspace's own playbooks/tools/docs against its weakest "
        "GTM categories. Recommend from the provided resource list; do not invent tools that aren't in the library."
    ),
    "insights": (
        "You are a proactive account-health monitor. Summarize what changed for this client since the last "
        "check-in, flag risks, and be concise — this is a digest, not a conversation."
    ),
}


def _get_workspace_chat_config(agent_type: str):
    return types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTIONS.get(agent_type, "You are a helpful GTM strategy assistant."),
        temperature=0.7,
        max_output_tokens=1536,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


def _summarize_context_for_prompt(agent_type: str, context: Dict[str, Any]) -> str:
    """Render the context dict into a compact text block for the Gemini prompt.
    Deliberately summarized rather than a raw dump, to keep prompts small and
    avoid leaking internal model instances (Resource/ActionItem objects aren't
    JSON-serializable and shouldn't reach the prompt directly)."""
    if agent_type == "portfolio":
        lines = [f"Workspace: {context['workspace_name']}", f"Completed assessments: {context['session_count']}"]
        if context["overall_delta"] is not None:
            lines.append(f"Overall score change: {context['overall_delta']:+.1f}")
        for row in context["category_deltas"][:5]:
            lines.append(f"- {row['category']}: {row['first']} -> {row['latest']} ({row['delta']:+.2f})")
        return "\n".join(lines)
    if agent_type == "resource":
        lines = [f"Workspace: {context['workspace_name']}", f"Resources in library: {context['resource_count']}"]
        lines.append(f"Currently weak categories: {', '.join(context['weakest_categories']) or 'none yet'}")
        if context["coverage_gaps"]:
            lines.append(f"Categories with no matching resource: {', '.join(context['coverage_gaps'])}")
        return "\n".join(lines)
    if agent_type == "insights":
        lines = [
            f"Workspace: {context['workspace_name']}",
            f"Open tasks: {context['open_action_count']} ({context['overdue_action_count']} overdue)",
            f"Pending AI suggestions awaiting review: {context['pending_suggestion_count']}",
        ]
        if context["recent_events"]:
            lines.append("Recent activity: " + "; ".join(
                e.summary or e.get_event_type_display() for e in context["recent_events"][:5]
            ))
        return "\n".join(lines)
    return ""


def handle_general_chat_workspace(
    agent_type: str,
    workspace: Workspace,
    context: Dict[str, Any],
    message: str,
) -> str:
    from .ai_services import (
        _extract_retry_delay_seconds,
        _is_quota_error,
        _quota_cooldown_active,
        _request_budget_available,
        _set_quota_cooldown,
    )

    if _quota_cooldown_active() or not _request_budget_available():
        return "I'm currently cooling down to stay within my API limits. Please try again in about 60 seconds."

    client = _get_chat_client()
    if not client:
        return "I'm having trouble connecting to my AI brain right now. Please try again in a moment."

    try:
        prompt = f"{_summarize_context_for_prompt(agent_type, context)}\n\nUser question: {message}"
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
            config=_get_workspace_chat_config(agent_type),
        )
        if response.text is None or not response.text.strip():
            return "I've processed your request but don't have anything further to add right now."
        return response.text.strip()
    except Exception as e:
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
            return "I've hit my temporary GTM strategy quota. Please try again shortly."
        log_ai_error(
            f"Workspace {agent_type} agent reasoning failure",
            e,
            service="google-genai",
            model="gemini-2.5-flash",
            extra={"workspace_id": str(workspace.id), "agent_type": agent_type},
        )
        return "I'm processing a lot of data right now. Please try asking again in a moment."


# ================================================================
# MAIN ENTRY POINT
# ================================================================

def process_workspace_chat_message(
    agent_type: str,
    workspace_id,
    message: str,
    user=None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Primary entry point for a workspace-scoped agent chat message."""
    if agent_type not in AGENT_TYPES:
        return {"success": False, "response": "Unknown agent type.", "intent": "error"}

    try:
        workspace = Workspace.objects.get(id=workspace_id)
        context = build_workspace_agent_context(agent_type, workspace)
        intent = detect_intent(agent_type, message)

        handler = DETERMINISTIC_HANDLERS.get(agent_type, {}).get(intent)
        if handler:
            response_text = handler(context, message)
        else:
            response_text = handle_general_chat_workspace(agent_type, workspace, context, message)

        return {"success": True, "response": response_text, "intent": intent}
    except Workspace.DoesNotExist:
        return {"success": False, "response": "Workspace not found.", "intent": "error"}
    except Exception as e:
        logger.error(f"Workspace {agent_type} chat processing error: {e}")
        return {
            "success": True,
            "response": "I'm having a bit of trouble with my reasoning loop. Your data is safe! Please try asking again shortly.",
            "intent": "error",
        }


# ================================================================
# SUGGESTED PROMPTS
# ================================================================

SUGGESTED_PROMPTS = {
    "portfolio": [
        "Show me the trend since our first assessment",
        "How has Delivery changed over time?",
        "Compare our latest vs first assessment",
    ],
    "resource": [
        "What resources do we have for our weakest area?",
        "Show me the resource library",
        "Where are our coverage gaps?",
    ],
    "insights": [
        "What happened this week?",
        "Do we have any overdue tasks?",
        "What suggestions are pending review?",
    ],
}


def get_suggested_prompts_for_agent(agent_type: str, workspace: Workspace) -> List[str]:
    return SUGGESTED_PROMPTS.get(agent_type, [])[:4]
