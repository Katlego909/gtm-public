"""Chat/tool layer for the three workspace-scoped agents: portfolio,
resource, and insights. Scoped to a Workspace (one client company) instead
of a single AssessmentSession. Uses real multi-turn memory
(client.chats.create(history=...)) and real Gemini function-calling
(GenerateContentConfig(tools=[...])) -- see _build_workspace_tools.
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
# CONTEXT-DRIVEN RESPONSE FORMATTERS
# ================================================================
# These used to be dispatched by a regex intent router; now they're wrapped
# as tool closures in _build_workspace_tools (called by real Gemini
# function-calling) and, for the insights agent, also reused directly by
# format_insights_digest_text for the scheduled/on-demand digest.
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


# ================================================================
# TOOLS (FUNCTION CALLING)
# ================================================================
# Tools are built per-request as closures bound to an already-authorized
# `workspace` -- no tool takes a workspace/session identifier as a
# model-facing parameter, so the model can call these but can never supply
# *which* workspace to act on. Each closure wraps one of the response
# formatters above, computed against a single fresh context per request.

def _make_draft_client_summary_tool(workspace: Workspace, user=None):
    """Shared closure factory for the draft_client_summary tool, made
    available on both the portfolio and insights agents (not resource --
    its job is finding docs/tools, not writing client prose)."""

    def draft_client_summary(focus: str = "") -> str:
        """Draft a polished, client-facing progress summary document for
        this workspace that the consultant can review and export (PDF or
        Word) to send directly to the client. ONLY call this when the user
        explicitly asks for a client summary, report, or something to
        send/share with the client -- do not call this just to describe
        what a summary would contain. `focus` is an optional area to
        emphasize, e.g. 'focus on Delivery'.
        """
        from .models import WorkspaceChatMessage

        try:
            summary_text = draft_client_summary_text(workspace, focus)
            chat_message = WorkspaceChatMessage.objects.create(
                workspace=workspace,
                agent_type=CLIENT_SUMMARY_AGENT_TYPE,
                user=user if user and getattr(user, "is_authenticated", False) else None,
                message="[System] Generate client summary" + (f" ({focus})" if focus else ""),
                response=summary_text,
                intent="client_summary",
            )
            return (
                f"I've drafted a client summary (ID {chat_message.pk}) -- you can review and export it "
                "as PDF or Word from the Generate Client Summary panel."
            )
        except Exception as e:
            logger.error(f"Client summary tool failure: {e}")
            return "I ran into a technical error drafting the client summary. Please try again."

    return draft_client_summary


def _build_workspace_tools(agent_type: str, workspace: Workspace, user=None) -> List[Any]:
    """Build the tool set for one workspace agent, scoped to `workspace`."""
    context = build_workspace_agent_context(agent_type, workspace)

    if agent_type == "portfolio":
        def get_trend_overview() -> str:
            """Get the overall GTM score trend across all completed
            assessments in this workspace. Use this when asked about
            trends, progress, or whether things have improved or declined
            over time.
            """
            return handle_trend_overview(context, "")

        def get_category_trend(category: str) -> str:
            """Get the trend for one specific GTM category (e.g. Demand,
            Conversion, Delivery) across all completed assessments. Use
            this when the user asks about a named category's trajectory.
            """
            return handle_category_trend(context, category)

        def compare_sessions() -> str:
            """Compare the workspace's latest completed assessment against
            its first one. Use this when asked to compare the latest vs
            first/earliest assessment.
            """
            return handle_session_comparison(context, "")

        def get_assessment_history() -> str:
            """List every completed assessment in this workspace with its
            date, overall score, and stage. Use this when asked for the
            full assessment history.
            """
            return handle_portfolio_summary(context, "")

        return [
            get_trend_overview,
            get_category_trend,
            compare_sessions,
            get_assessment_history,
            _make_draft_client_summary_tool(workspace, user),
        ]

    if agent_type == "resource":
        def find_resource(query: str = "") -> str:
            """Search the workspace's resource library for a document,
            deck, or tool matching a topic. Use this when asked whether a
            specific resource exists.
            """
            return handle_find_resource(context, query)

        def recommend_resource_for_gap() -> str:
            """Recommend resources from the library that address the
            workspace's currently weakest GTM categories. Use this when
            asked what to use for a weak area, or for a recommendation.
            """
            return handle_recommend_for_gap(context, "")

        def list_resources() -> str:
            """List all resources currently in the workspace's library."""
            return handle_list_resources(context, "")

        def get_coverage_gaps() -> str:
            """Identify weak GTM categories that have no matching resource
            in the library. Use this when asked about coverage gaps or
            missing resources.
            """
            return handle_coverage_gap(context, "")

        return [find_resource, recommend_resource_for_gap, list_resources, get_coverage_gaps]

    if agent_type == "insights":
        def get_score_change() -> str:
            """Get the workspace's score trend/change since its first
            assessment. Use this when asked what changed with scores.
            """
            return handle_score_change(context, "")

        def get_activity_summary() -> str:
            """Get a summary of recent workspace activity (tasks, comments,
            resources). Use this when asked what happened recently.
            """
            return handle_activity_summary(context, "")

        def get_pending_reviews() -> str:
            """List AI-suggested gap metric changes awaiting human review.
            Use this when asked about pending suggestions or approvals.
            """
            return handle_pending_review(context, "")

        def get_overdue_tasks() -> str:
            """List overdue action items in this workspace. Use this when
            asked about overdue or stale tasks.
            """
            return handle_overdue_tasks(context, "")

        return [
            get_score_change,
            get_activity_summary,
            get_pending_reviews,
            get_overdue_tasks,
            _make_draft_client_summary_tool(workspace, user),
        ]

    return []


# ================================================================
# GEMINI CONVERSATIONAL AGENT (real multi-turn memory + tool-calling,
# reuses ai_chat's cached Vertex AI client)
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


def _get_workspace_chat_config(agent_type: str, tools: Optional[List[Any]] = None):
    base_instruction = SYSTEM_INSTRUCTIONS.get(agent_type, "You are a helpful GTM strategy assistant.")
    tool_guidance = (
        "\n\nGround every claim in real workspace data -- call the appropriate tool to fetch it rather than "
        "guessing or making up numbers. If a tool exists that answers the user's question, call it before answering."
    )
    return types.GenerateContentConfig(
        system_instruction=base_instruction + tool_guidance,
        temperature=0.7,
        max_output_tokens=1536,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        tools=tools or None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=4),
    )


def _build_workspace_chat_history(workspace: Workspace, agent_type: str, max_turns: int = 12) -> List[Any]:
    """Reconstruct this agent's recent conversation as types.Content history.
    Excludes synthetic system rows (insights_digest/client_summary) so the
    model never sees a turn the user didn't actually say."""
    from .agent_runtime import build_content_history
    from .models import WorkspaceChatMessage

    rows = list(reversed(
        WorkspaceChatMessage.objects.filter(workspace=workspace, agent_type=agent_type)
        .exclude(intent__in=["insights_digest", "client_summary"])
        .order_by('-created_at')[:max_turns]
    ))
    return build_content_history(rows, max_turns=max_turns)


def handle_general_chat_workspace(
    agent_type: str,
    workspace: Workspace,
    message: str,
    user=None,
) -> str:
    """The workspace agent's conversational path: real multi-turn memory +
    real Gemini tool-calling via the tools built by _build_workspace_tools."""
    from .ai_services import (
        _extract_retry_delay_seconds,
        _is_quota_error,
        _quota_cooldown_active,
        _request_budget_available,
        _set_quota_cooldown,
    )
    from .agent_runtime import run_agent_turn

    if _quota_cooldown_active() or not _request_budget_available():
        return "I'm currently cooling down to stay within my API limits. Please try again in about 60 seconds."

    client = _get_chat_client()
    if not client:
        return "I'm having trouble connecting to my AI brain right now. Please try again in a moment."

    try:
        history = _build_workspace_chat_history(workspace, agent_type)
        tools = _build_workspace_tools(agent_type, workspace, user=user)
        config = _get_workspace_chat_config(agent_type, tools=tools)

        text = run_agent_turn(
            client=client,
            model="gemini-2.5-flash",
            config=config,
            history=history,
            message=message,
            usage_label=f"workspace_agent_{agent_type}",
        )
        return text or "I've processed your request but don't have anything further to add right now."
    except Exception as e:
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
            return "I've hit my temporary GTM strategy quota. Please try again shortly. If I'd already started anything before hitting the limit, it's saved."
        log_ai_error(
            f"Workspace {agent_type} agent reasoning failure",
            e,
            service="google-genai",
            model="gemini-2.5-flash",
            extra={"workspace_id": str(workspace.id), "agent_type": agent_type},
        )
        return "I'm processing a lot of data right now. Please try asking again in a moment."


# ================================================================
# CLIENT SUMMARY (client-facing deliverable) -- one dedicated one-shot
# generation call, not routed through the conversational chat/tools loop.
# Pure generation, no persistence -- callers decide where to store the
# result (WorkspaceChatMessage with intent="client_summary").
# ================================================================

# Persisted with this agent_type regardless of which agent's tool (or the
# button-triggered background job) generated it, so exports don't need to
# know which agent produced the summary.
CLIENT_SUMMARY_AGENT_TYPE = "insights"

_CLIENT_SUMMARY_SYSTEM_INSTRUCTION = """You are drafting a client-facing GTM progress summary for a
consultant to send directly to their client. Write for the client, not for internal use:
- No internal jargon -- don't say "GTM score" without context, don't mention "AI", "agent", or internal tooling.
- Professional, confident, and encouraging in tone -- this is relationship-building content.
- Structure as markdown with these sections: Executive Summary, Progress Since Last Review, Key Wins,
  Areas of Focus, Recommended Next Steps.
- Be concrete and specific using only the data provided; never invent facts or numbers not present in the context.
- Keep it concise -- this should read in under two minutes."""


def _get_client_summary_config():
    return types.GenerateContentConfig(
        system_instruction=_CLIENT_SUMMARY_SYSTEM_INSTRUCTION,
        temperature=0.5,
        max_output_tokens=3072,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


def _summarize_client_context(workspace: Workspace) -> str:
    """Compact, plain-text summary of workspace data for the client-summary
    prompt -- reuses the same read-only aggregation as the other agents."""
    trends = build_portfolio_trends(workspace)
    resource_matches = build_resource_gap_matches(workspace)
    digest = build_workspace_insights_digest(workspace)

    lines = [f"Client: {workspace.name}", f"Completed assessments: {trends['session_count']}"]
    if trends["latest_overall"] is not None:
        lines.append(f"Latest overall score: {trends['latest_overall']}/100")
    if trends["overall_delta"] is not None:
        lines.append(f"Overall score change since first assessment: {trends['overall_delta']:+.1f}")
    for row in trends["category_deltas"][:5]:
        lines.append(f"- {row['category']}: {row['first']} -> {row['latest']} ({row['delta']:+.2f})")

    if resource_matches["unrecommended_resources"]:
        lines.append("Available resources not yet leveraged: " + ", ".join(
            r.name for r in resource_matches["unrecommended_resources"][:5]
        ))

    lines.append(f"Open tasks: {digest['open_action_count']} ({digest['overdue_action_count']} overdue)")
    if digest["recent_events"]:
        lines.append("Recent activity: " + "; ".join(
            e.summary or e.get_event_type_display() for e in digest["recent_events"][:5]
        ))

    return "\n".join(lines)


def draft_client_summary_text(workspace: Workspace, focus: str = "") -> str:
    """Draft a polished, client-facing progress summary for this workspace.

    Pure generation -- does not persist anything or touch quota-cooldown
    state; callers (the background refresh job, or the conversational tool)
    are responsible for the quota gate, persistence, and error handling
    appropriate to their trigger path.
    """
    client = _get_chat_client()
    if not client:
        raise RuntimeError("AI client unavailable")

    prompt = _summarize_client_context(workspace)
    if focus:
        prompt += f"\n\nSpecial focus requested for this summary: {focus}"

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[types.Content(role="user", parts=[types.Part.from_text(text=prompt)])],
        config=_get_client_summary_config(),
    )
    if response.text is None or not response.text.strip():
        raise RuntimeError("Empty response from client summary generation")
    return response.text.strip()


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
        response_text = handle_general_chat_workspace(agent_type, workspace, message, user=user)
        return {"success": True, "response": response_text, "intent": "general_chat"}
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
