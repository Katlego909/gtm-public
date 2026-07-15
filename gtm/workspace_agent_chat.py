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

def _make_draft_client_summary_tool(agent_type: str, workspace: Workspace, user=None):
    """Shared closure factory for the draft_client_summary tool, made
    available on both the portfolio and insights agents (not resource --
    its job is finding docs/tools, not writing client prose). Uses the
    dedicated, carefully-tuned draft_client_summary_text generator below,
    then saves the result as an AgentDocument like any other document."""

    def draft_client_summary(focus: str = "") -> str:
        """Draft a polished, client-facing progress summary document for
        this workspace and save it, so the consultant can review and
        export it as PDF, Word, or Markdown. ONLY call this when the user
        explicitly asks for a client summary, report, or something to
        send/share with the client -- do not call this just to describe
        what a summary would contain. `focus` is an optional area to
        emphasize, e.g. 'focus on Delivery'.
        """
        from .agent_documents import create_agent_document

        try:
            summary_text = draft_client_summary_text(workspace, focus)
            title = f"Client Summary - {workspace.name}" + (f" ({focus})" if focus else "")
            doc = create_agent_document(
                agent_type=agent_type,
                title=title,
                content=summary_text,
                doc_type="client_summary",
                workspace=workspace,
                user=user,
            )
            return (
                f"I've drafted a client summary (ID {doc.pk}) -- you can review and export it "
                "as PDF, Word, or Markdown from the Documents panel."
            )
        except Exception as e:
            logger.error(f"Client summary tool failure: {e}")
            return "I ran into a technical error drafting the client summary. Please try again."

    return draft_client_summary


def _build_document_tools(agent_type: str, workspace: Workspace, user=None) -> List[Any]:
    """Generic create/edit/list document tools, available to every workspace
    agent (not just the ones with a specialized draft_client_summary)."""
    from .agent_documents import create_agent_document, edit_agent_document, list_agent_documents

    def create_document(title: str, content: str, doc_type: str = "other") -> str:
        """Create and save a new document (e.g. an action plan, roadmap, or
        resource brief) that the user can review and export as PDF, Word,
        or Markdown. `doc_type` should be one of: client_summary,
        action_plan, roadmap, resource_brief, other.
        """
        doc = create_agent_document(
            agent_type=agent_type, title=title, content=content, doc_type=doc_type,
            workspace=workspace, user=user,
        )
        return f'Saved "{doc.title}" (ID {doc.pk}) -- you can review and export it from the Documents panel.'

    def edit_document(document_id: str, new_content: str) -> str:
        """Edit an existing document's content by its ID (use list_documents
        first if you don't already know the ID). Replaces the document's
        full content and saves a new version.
        """
        doc = edit_agent_document(document_id, new_content, workspace=workspace)
        if not doc:
            return "I couldn't find a document with that ID in this workspace."
        return f'Updated "{doc.title}" to version {doc.version}.'

    def list_documents(doc_type: str = "") -> str:
        """List documents already saved for this workspace, optionally
        filtered by doc_type. Use this to find a document's ID before
        editing it, or to check what's already been created.
        """
        docs = list(list_agent_documents(workspace=workspace, doc_type=doc_type or None)[:10])
        if not docs:
            return "No documents have been saved for this workspace yet."
        lines = ["Documents in this workspace:"]
        for d in docs:
            lines.append(f"- [{d.pk}] {d.title} ({d.get_doc_type_display()}, v{d.version}, updated {d.updated_at.strftime('%b %d, %Y')})")
        return "\n".join(lines)

    return [create_document, edit_document, list_documents]


def _make_consult_tool(target_agent_type: str, workspace: Workspace, user=None, _handoff_depth: int = 0):
    """Build one consult_<target>_agent tool that hands the question off to
    another workspace agent's full entry point (real memory + real tools),
    and persists the exchange into the target agent's own conversation log
    so it genuinely remembers being consulted."""
    from .agent_runtime import AGENT_DIRECTORY

    info = AGENT_DIRECTORY.get(target_agent_type, {})
    name = info.get("name", target_agent_type)
    label = info.get("label", target_agent_type)
    domain = info.get("domain", "")

    def consult_tool(question: str) -> str:
        from .models import WorkspaceChatMessage

        try:
            answer = handle_general_chat_workspace(
                target_agent_type, workspace, question, user=user, _handoff_depth=_handoff_depth + 1
            )
            WorkspaceChatMessage.objects.create(
                workspace=workspace,
                agent_type=target_agent_type,
                user=user if user and getattr(user, "is_authenticated", False) else None,
                message=question,
                response=answer,
                intent="handoff_query",
            )
            return answer
        except Exception as e:
            logger.error(f"Handoff to {target_agent_type} failed: {e}")
            return f"I couldn't reach {name} right now."

    consult_tool.__name__ = f"consult_{target_agent_type}_agent"
    consult_tool.__doc__ = (
        f"Consult {name} ({label}), who specializes in: {domain} Use this when the user's question "
        "is really about that domain rather than yours. Pass the specific question to ask."
    )
    return consult_tool


def _build_consult_tools(agent_type: str, workspace: Workspace, user=None, _handoff_depth: int = 0) -> List[Any]:
    """Build consult tools to this agent's peers, depth-gated so a handoff
    chain is guaranteed to terminate (see agent_runtime.MAX_HANDOFF_DEPTH)."""
    from .agent_runtime import MAX_HANDOFF_DEPTH

    if _handoff_depth >= MAX_HANDOFF_DEPTH:
        return []

    peers = [t for t in AGENT_TYPES if t != agent_type]
    return [_make_consult_tool(peer, workspace, user, _handoff_depth) for peer in peers]


def _build_workspace_tools(
    agent_type: str,
    workspace: Workspace,
    user=None,
    _handoff_depth: int = 0,
    include_consult: bool = True,
) -> List[Any]:
    """Build the tool set for one workspace agent, scoped to `workspace`.

    `include_consult=False` omits the consult_* tools -- used by Team mode
    (gtm/team_chat.py), which gives an agent transfer_to_* tools instead.
    The two mechanisms are deliberately mutually exclusive per agent turn.
    """
    from .agent_actions import build_agent_action_tools

    context = build_workspace_agent_context(agent_type, workspace)
    document_tools = _build_document_tools(agent_type, workspace, user=user)
    action_tools = build_agent_action_tools(workspace, user) if user else []
    consult_tools = (
        _build_consult_tools(agent_type, workspace, user=user, _handoff_depth=_handoff_depth)
        if include_consult else []
    )

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
            _make_draft_client_summary_tool("portfolio", workspace, user),
            *document_tools,
            *action_tools,
            *consult_tools,
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

        return [
            find_resource,
            recommend_resource_for_gap,
            list_resources,
            get_coverage_gaps,
            *document_tools,
            *action_tools,
            *consult_tools,
        ]

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
            _make_draft_client_summary_tool("insights", workspace, user),
            *document_tools,
            *action_tools,
            *consult_tools,
        ]

    return []


# ================================================================
# GEMINI CONVERSATIONAL AGENT (real multi-turn memory + tool-calling,
# reuses ai_chat's cached Vertex AI client)
# ================================================================

SYSTEM_INSTRUCTIONS = {
    "portfolio": (
        "You are Nora, a GTM portfolio analyst reviewing one client's full assessment history over time. "
        "Focus on trajectory, not a single snapshot. Be conversational, direct, and concise."
    ),
    "resource": (
        "You are Theo, a resource curator surfacing the workspace's own playbooks/tools/docs against its weakest "
        "GTM categories. Recommend from the provided resource list; do not invent tools that aren't in the library."
    ),
    "insights": (
        "You are Milo, a proactive account-health monitor. Summarize what changed for this client since the last "
        "check-in, flag risks, and be concise — this is a digest, not a conversation."
    ),
}


def _get_workspace_chat_config(agent_type: str, tools: Optional[List[Any]] = None):
    from .agent_runtime import build_agent_directory_prompt

    base_instruction = SYSTEM_INSTRUCTIONS.get(agent_type, "You are a helpful GTM strategy assistant.")
    tool_guidance = (
        "\n\nGround every claim in real workspace data -- call the appropriate tool to fetch it rather than "
        "guessing or making up numbers. If a tool exists that answers the user's question, call it before "
        "answering. You can also create, edit, and save documents (action plans, roadmaps, summaries, briefs) "
        "that the user can export as PDF, Word, or Markdown -- use create_document/edit_document/list_documents.\n\n"
        "Some turns in your history were said by other specialist agents on this team, not the user -- the "
        "system automatically marks whose turn is whose when it loads your history, so you never need to and "
        "must never add that marking yourself; write your own replies as plain prose with no name or bracket "
        "in front of them. Treat a teammate's marked turn as background you're aware of, not as an answer to "
        "reuse: if the user's current question needs specific data -- a name, a number, anything not identical "
        "to what a teammate already looked up -- call the right tool yourself and get a fresh answer rather "
        "than repeating or lightly rewording something a teammate said about a different question."
    )
    system_instruction = base_instruction + tool_guidance

    # Only mention handoff capability when a consult_ tool is actually in
    # this turn's tool list -- a depth-capped sub-agent has none, and a
    # prompt claiming collaboration it can't act on would be misleading.
    has_handoff_tools = any(getattr(t, "__name__", "").startswith("consult_") for t in (tools or []))
    if has_handoff_tools:
        system_instruction += build_agent_directory_prompt(agent_type)

    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=0.7,
        max_output_tokens=1536,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        tools=tools or None,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=4),
    )


def _build_workspace_chat_history(workspace: Workspace, agent_type: str, max_turns: int = 16) -> List[Any]:
    """Reconstruct this workspace's shared, cross-agent conversation as
    types.Content history -- every agent's turns are visible to every other
    agent (speaker-tagged via AGENT_DIRECTORY), not just this agent's own.
    This is what closes the "Portfolio hands off to Resource but doesn't
    remember doing so" gap: the exchange was always persisted into
    Resource's log, it just wasn't being read back into Portfolio's own
    context until now.

    Excludes synthetic system rows (insights_digest, and legacy
    client_summary rows from before documents had their own model) so the
    model never sees a turn nobody actually said. Handoff exchanges
    (intent="handoff_query") are real conversational content and are NOT
    excluded.
    """
    from .agent_runtime import AGENT_DIRECTORY, build_tagged_content_history
    from .models import WorkspaceChatMessage

    rows = list(reversed(
        WorkspaceChatMessage.objects.filter(workspace=workspace)
        .exclude(intent__in=["insights_digest", "client_summary"])
        .order_by('-created_at')[:max_turns]
    ))
    entries = [
        (
            None if row.agent_type == agent_type else AGENT_DIRECTORY.get(row.agent_type, {}).get("name"),
            row.message,
            row.response,
        )
        for row in rows
    ]
    return build_tagged_content_history(entries, max_turns=max_turns)


def handle_general_chat_workspace(
    agent_type: str,
    workspace: Workspace,
    message: str,
    user=None,
    _handoff_depth: int = 0,
    supplemental_context: str = "",
) -> str:
    """The workspace agent's conversational path: real multi-turn memory +
    real Gemini tool-calling via the tools built by _build_workspace_tools.

    `_handoff_depth` is not model-facing -- it's incremented by consult
    tools when this function is called recursively as a handoff target, and
    caps how many further hops that sub-agent can itself hand off to (see
    agent_runtime.MAX_HANDOFF_DEPTH).

    `supplemental_context` is extracted text from this turn's uploaded
    attachments (see dashboard/document_processors.py's
    _process_agent_attachments) -- folded into the message sent to the
    model but not into `message` itself, so the persisted/displayed chat
    bubble stays the clean text the user actually typed."""
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
        full_message = message
        if supplemental_context:
            full_message = f"{message}\n\n[Relevant attachment context]\n{supplemental_context}"

        history = _build_workspace_chat_history(workspace, agent_type)
        tools = _build_workspace_tools(agent_type, workspace, user=user, _handoff_depth=_handoff_depth)
        config = _get_workspace_chat_config(agent_type, tools=tools)

        text = run_agent_turn(
            client=client,
            model="gemini-2.5-flash",
            config=config,
            history=history,
            message=full_message,
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
# CLIENT SUMMARY GENERATOR -- one dedicated one-shot generation call with
# its own carefully-tuned prompt, not routed through the conversational
# chat/tools loop. Pure generation, no persistence -- the
# draft_client_summary tool (in _make_draft_client_summary_tool above)
# saves the result as an AgentDocument.
# ================================================================

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
    supplemental_context: str = "",
) -> Dict[str, Any]:
    """Primary entry point for a workspace-scoped agent chat message."""
    if agent_type not in AGENT_TYPES:
        return {"success": False, "response": "Unknown agent type.", "intent": "error"}

    try:
        workspace = Workspace.objects.get(id=workspace_id)
        response_text = handle_general_chat_workspace(
            agent_type, workspace, message, user=user, supplemental_context=supplemental_context
        )
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
