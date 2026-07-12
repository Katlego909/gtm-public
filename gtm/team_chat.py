"""Team Chat: one window where control genuinely transfers between all 4
agents, matching ADK's transfer_to_agent semantics (as distinct from the
Agent-as-a-Tool "consult" pattern used by the 4 individual tabs).

Agent-as-a-Tool (gtm/ai_chat.py's consult_* tools, gtm/workspace_agent_chat.py's
consult_* tools): the calling agent stays in charge, gets a text answer back,
and paraphrases it into its own reply.

Sub-agent transfer (this module): responsibility for the whole conversation
passes to the target agent, which replies in its own voice using its own
system prompt and tools. The two mechanisms are deliberately mutually
exclusive per turn -- an agent operating in Team mode gets transfer_to_*
tools instead of consult_* tools (see include_consult=False on
_build_session_tools/_build_workspace_tools).

Because "stop the moment a transfer is requested, instead of letting the
model paraphrase it" isn't something the SDK's automatic function-calling
loop can do (it only ever returns final synthesized text), this module runs
a manual function-calling loop: automatic_function_calling is disabled,
non-transfer tool calls are executed and fed back by hand, and the instant a
transfer_to_* call appears, the loop stops and reports it instead of
executing it.
"""

import logging
from typing import Any, Dict, List, Optional

from .ai_chat import GENAI_AVAILABLE, GTM_STRATEGIST_SYSTEM_INSTRUCTION, _build_session_tools, _get_chat_client
from .models import AssessmentSession
from .models_workspace import Workspace
from .utils_logging import log_ai_error
from .workspace_agent_chat import SYSTEM_INSTRUCTIONS, _build_workspace_tools

logger = logging.getLogger(__name__)

try:
    from google.genai import types
except ImportError:
    types = None

TEAM_AGENT_TYPES = ("gtm_strategist", "portfolio", "resource", "insights")

# Cap on transfers within a single incoming user message -- mirrors
# agent_runtime.MAX_HANDOFF_DEPTH's role for the consult-tool mesh. Without
# this, two agents could bounce a conversation back and forth indefinitely.
MAX_TRANSFERS = 2

# Manual function-calling round cap per single agent turn -- mirrors the
# maximum_remote_calls=4 used everywhere else in this app via automatic
# function calling; here we enforce it ourselves since automatic calling is
# off for Team mode.
MAX_TOOL_ROUNDS = 4


# ================================================================
# SHARED, SPEAKER-TAGGED TIMELINE (read-side; reuses the same persisted
# ChatMessage/WorkspaceChatMessage rows the 4 individual tabs already write)
# ================================================================

def _collect_team_timeline(workspace: Optional[Workspace], session: Optional[AssessmentSession], max_turns: int = 16):
    """Merge this workspace's WorkspaceChatMessage rows and this session's
    ChatMessage rows into one chronological, agent-tagged timeline. Unlike
    the individual tabs' history builders (which omit the tag for an
    agent's own past turns), every row here is tagged with its real speaker
    -- in Team mode there's no single "me" perspective, since who's active
    changes turn to turn."""
    from .models import ChatMessage, WorkspaceChatMessage

    entries = []
    if workspace is not None:
        rows = list(
            WorkspaceChatMessage.objects.filter(workspace=workspace)
            .exclude(intent__in=["insights_digest", "client_summary"])
            .order_by('-created_at')[:max_turns]
        )
        entries += [(row.agent_type, row.created_at, row.message, row.response) for row in rows]

    if session is not None:
        rows = list(ChatMessage.objects.filter(session=session).order_by('-created_at')[:max_turns])
        entries += [("gtm_strategist", row.created_at, row.message, row.response) for row in rows]

    entries.sort(key=lambda entry: entry[1])
    return entries[-max_turns:]


def _resolve_active_agent_type(workspace: Optional[Workspace], session: Optional[AssessmentSession]) -> str:
    """Whoever most recently responded in the shared timeline is who's
    currently active; a brand-new conversation defaults to the GTM
    Strategist if a session is bound, else the Insights agent (a reasonable
    "whole workspace" starting point when there's no single assessment in
    play)."""
    timeline = _collect_team_timeline(workspace, session)
    if timeline:
        return timeline[-1][0]
    return "gtm_strategist" if session is not None else "insights"


def _build_team_history(
    workspace: Optional[Workspace],
    session: Optional[AssessmentSession],
    viewer_agent_type: str,
    max_turns: int = 16,
) -> List[Any]:
    """Build history for whichever agent (`viewer_agent_type`) is about to
    generate this turn. Every OTHER agent's rows get tagged; this agent's
    OWN past rows do NOT (same rule as the individual tabs in Part A) --
    tagging an agent's own history with its own name teaches it, by literal
    pattern-continuation, to keep prefixing that tag onto its new replies
    too, overriding the explicit instruction not to. Confirmed by testing:
    self-tagging in Team mode caused exactly that leak."""
    from .agent_runtime import AGENT_DIRECTORY, build_tagged_content_history

    timeline = _collect_team_timeline(workspace, session, max_turns=max_turns)
    tagged = [
        (
            None if agent_type == viewer_agent_type else AGENT_DIRECTORY.get(agent_type, {}).get("name"),
            message,
            response,
        )
        for agent_type, _created_at, message, response in timeline
    ]
    return build_tagged_content_history(tagged, max_turns=max_turns)


# ================================================================
# TRANSFER TOOLS
# ================================================================

def _make_transfer_tool(target_agent_type: str):
    """Build one transfer_to_<target>_agent tool. Its body is never actually
    executed by the manual loop below -- the loop intercepts a call to a
    name starting with "transfer_to_" before it would run, and treats it as
    a signal to switch the active agent. The function still needs a real,
    correctly-named/documented body so the SDK can build a valid tool
    schema from it."""
    from .agent_runtime import AGENT_DIRECTORY

    info = AGENT_DIRECTORY.get(target_agent_type, {})
    name = info.get("name", target_agent_type)
    label = info.get("label", target_agent_type)
    domain = info.get("domain", "")

    def transfer_tool(reason: str = "") -> str:
        return f"Transferring to {name} ({label}): {reason}"

    transfer_tool.__name__ = f"transfer_to_{target_agent_type}_agent"
    transfer_tool.__doc__ = (
        f"Transfer this entire conversation to {name} ({label}), who specializes in: {domain} "
        "Use this when the user's need is really owned by that agent going forward, not just a "
        "one-off fact you need right now -- after transferring, that agent handles all further "
        "messages until it (or the user) moves on. Briefly say why in `reason`."
    )
    return transfer_tool


def _agent_type_from_transfer_tool_name(name: str) -> str:
    return name.replace("transfer_to_", "", 1).removesuffix("_agent")


def _build_transfer_tools(agent_type: str, has_session: bool) -> List[Any]:
    targets = [t for t in TEAM_AGENT_TYPES if t != agent_type]
    if not has_session:
        targets = [t for t in targets if t != "gtm_strategist"]
    return [_make_transfer_tool(t) for t in targets]


# ================================================================
# CONFIG
# ================================================================

def _build_team_directory_prompt(current_agent_type: str) -> str:
    """Team-mode variant of agent_runtime.build_agent_directory_prompt --
    that function's wording references "consult_" tools, which Team mode
    doesn't have (it has transfer_to_* instead), so it can't be reused
    verbatim here without giving the model instructions for a tool that
    doesn't exist in this mode."""
    from .agent_runtime import AGENT_DIRECTORY

    others = [
        f"- {info['name']} ({info['label']}): {info['domain']}"
        for agent_type, info in AGENT_DIRECTORY.items()
        if agent_type != current_agent_type
    ]
    return (
        "\n\nYou are the currently active agent in a shared Team Chat window -- the user is talking "
        "directly to you, not through an intermediary. Your teammates on this team:\n"
        + "\n".join(others)
        + "\n\nIf the user's need is really owned by a teammate going forward (not just a one-off fact "
        "you could look up yourself), call the matching transfer_to_ tool to hand them the whole "
        "conversation. Otherwise answer directly using your own tools.\n\n"
        "Some turns in your history were said by other agents on this team, not the user -- the system "
        "automatically marks whose turn is whose when it loads your history, so you never need to and "
        "must never add that marking yourself; write your own replies as plain prose with no name or "
        "bracket in front of them. A teammate's marked turn is background you're aware of, not an answer "
        "to reuse or a substitute for actually handling the current request: if this question needs "
        "specific data or genuinely belongs to that teammate's domain, either call your own tool for "
        "fresh data or transfer to them -- don't just repeat or reword what they said about a different "
        "question."
    )


def _get_team_config(agent_type: str, tools: List[Any]):
    if agent_type == "gtm_strategist":
        base_instruction = GTM_STRATEGIST_SYSTEM_INSTRUCTION
        temperature = 0.8
        max_output_tokens = 2048
    else:
        base_instruction = SYSTEM_INSTRUCTIONS.get(agent_type, "You are a helpful GTM strategy assistant.")
        temperature = 0.7
        max_output_tokens = 1536

    system_instruction = base_instruction + _build_team_directory_prompt(agent_type)

    return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        tools=tools or None,
        # Automatic function calling is deliberately OFF for Team mode --
        # see module docstring. The manual loop below enforces its own cap
        # (MAX_TOOL_ROUNDS) in place of maximum_remote_calls.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


# ================================================================
# MANUAL FUNCTION-CALLING LOOP
# ================================================================

def _call_tool(name: str, args: Optional[Dict[str, Any]], tool_map: Dict[str, Any]) -> str:
    fn = tool_map.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        return fn(**(args or {}))
    except Exception as e:
        logger.error(f"Team tool call failed ({name}): {e}")
        return f"Tool {name} failed: {e}"


def _run_team_turn(agent_type, workspace, session, message, user, allow_transfer):
    """Run one agent's turn in Team mode via a manual function-calling
    loop. Returns (agent_type, text_or_None, transfer_target_or_None,
    transfer_reason_or_None) -- text is None exactly when a transfer was
    requested, and vice versa."""
    from .ai_services import (
        _extract_retry_delay_seconds,
        _is_quota_error,
        _quota_cooldown_active,
        _request_budget_available,
        _set_quota_cooldown,
    )

    if _quota_cooldown_active() or not _request_budget_available():
        return agent_type, "I'm currently cooling down to stay within my API limits. Please try again in about 60 seconds.", None, None

    client = _get_chat_client()
    if not client:
        return agent_type, "I'm having trouble connecting to my AI brain right now. Please try again in a moment.", None, None

    if agent_type == "gtm_strategist":
        domain_tools = _build_session_tools(session, user=user, include_consult=False) if session else []
    else:
        domain_tools = _build_workspace_tools(agent_type, workspace, user=user, include_consult=False)

    transfer_tools = _build_transfer_tools(agent_type, has_session=session is not None) if allow_transfer else []
    tool_map = {fn.__name__: fn for fn in domain_tools}  # transfer tools intentionally excluded -- never executed

    history = _build_team_history(workspace, session, viewer_agent_type=agent_type)
    config = _get_team_config(agent_type, tools=domain_tools + transfer_tools)

    try:
        chat = client.chats.create(model="gemini-2.5-flash", config=config, history=history)
        response = chat.send_message(message)

        for _ in range(MAX_TOOL_ROUNDS):
            calls = list(getattr(response, "function_calls", None) or [])
            if not calls:
                text = (response.text or "").strip()
                return agent_type, text or "I've processed your request but don't have anything further to add right now.", None, None

            transfer_call = next((c for c in calls if c.name.startswith("transfer_to_")), None)
            if transfer_call:
                target = _agent_type_from_transfer_tool_name(transfer_call.name)
                reason = (transfer_call.args or {}).get("reason", "")
                return agent_type, None, target, reason

            parts = [
                types.Part.from_function_response(
                    name=call.name,
                    response={"result": _call_tool(call.name, call.args, tool_map)},
                )
                for call in calls
            ]
            response = chat.send_message(parts)

        # Exhausted the manual round cap without a final text answer.
        text = (response.text or "").strip()
        return agent_type, text or "I've processed your request but don't have anything further to add right now.", None, None

    except Exception as e:
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
            return agent_type, "I've hit my temporary GTM strategy quota. Please try again shortly.", None, None
        log_ai_error(
            f"Team chat turn failure ({agent_type})",
            e,
            service="google-genai",
            model="gemini-2.5-flash",
            extra={"workspace_id": str(workspace.id) if workspace else None, "agent_type": agent_type},
        )
        return agent_type, "I'm processing a lot of data right now. Please try asking again in a moment.", None, None


def _persist_team_turn(agent_type, workspace, session, message, text, user, attachments=None):
    """Persist into exactly the same table the responding agent would use
    if talked to directly -- so a Team-tab exchange is immediately visible
    from that agent's own tab too (same underlying storage, no new model)."""
    if agent_type == "gtm_strategist":
        if session is None:
            return
        from .models import ChatMessage
        ChatMessage.objects.create(
            session=session,
            user=user if user and getattr(user, "is_authenticated", False) else None,
            message=message,
            response=text,
            attachments=attachments or [],
            intent="general_chat",
        )
    else:
        if workspace is None:
            return
        from .models import WorkspaceChatMessage
        WorkspaceChatMessage.objects.create(
            workspace=workspace,
            session=session,
            agent_type=agent_type,
            user=user if user and getattr(user, "is_authenticated", False) else None,
            message=message,
            response=text,
            attachments=attachments or [],
            intent="general_chat",
        )


# ================================================================
# MAIN ENTRY POINT
# ================================================================

def process_team_chat_message(
    workspace_id,
    message: str,
    user=None,
    session_id: Optional[str] = None,
    max_transfers: int = MAX_TRANSFERS,
    supplemental_context: str = "",
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Primary entry point for a Team Chat message. Resolves who's
    currently active, runs their turn, and follows any transfer chain
    (capped at max_transfers) until an agent produces real text.

    `supplemental_context` is extracted attachment text folded into the
    model-facing message (survives transfers, so a teammate who receives
    the conversation still sees what was attached); `attachments` is the
    structured metadata persisted alongside the clean, unmodified
    `message` once an agent produces a final answer."""
    try:
        workspace = Workspace.objects.filter(id=workspace_id).first() if workspace_id else None
        session = AssessmentSession.objects.filter(uuid=session_id).first() if session_id else None
        if workspace is None and session is None:
            return {"success": False, "response": "Select a workspace or assessment first.", "agent_type": None}

        active_type = _resolve_active_agent_type(workspace, session)
        llm_message = message
        if supplemental_context:
            llm_message = f"{message}\n\n[Relevant attachment context]\n{supplemental_context}"
        current_message = llm_message
        transfers = 0

        while True:
            agent_type, text, transfer_target, transfer_reason = _run_team_turn(
                active_type, workspace, session, current_message, user,
                allow_transfer=(transfers < max_transfers),
            )
            if transfer_target:
                transfers += 1
                active_type = transfer_target
                # Always carry the real original question (plus any
                # attachment context) forward -- the transferring agent's
                # `reason` is supplementary context, not a replacement.
                # Losing the original question to a possibly-vague summary
                # is what let the target agent drift toward stale/
                # similar-sounding entities from its own shared-history
                # view instead of the actual current ask.
                current_message = llm_message
                if transfer_reason:
                    current_message += f"\n\n[A teammate handed this to you, noting: {transfer_reason}]"
                continue

            _persist_team_turn(agent_type, workspace, session, message, text, user, attachments=attachments)
            return {"success": True, "response": text, "agent_type": agent_type}

    except Exception as e:
        logger.error(f"Team chat processing error: {e}")
        return {
            "success": True,
            "response": "I'm having a bit of trouble with my reasoning loop. Your data is safe! Please try asking again shortly.",
            "agent_type": None,
        }
