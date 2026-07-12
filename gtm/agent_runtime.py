"""Shared mechanical helpers for running a Gemini chat turn with real
multi-turn memory. Houses only the pieces that are byte-for-byte identical
across gtm/ai_chat.py and gtm/workspace_agent_chat.py -- system prompts, tool
lists, and quota-error handling stay local to each agent module since they
already diverge per agent.
"""

import logging
from typing import Any, List, Optional, Sequence

logger = logging.getLogger(__name__)

try:
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False

try:
    from .utils_ai_monitoring import AIUsageTracker
    MONITORING_AVAILABLE = True
except ImportError:
    MONITORING_AVAILABLE = False


# ================================================================
# CROSS-AGENT HANDOFF
# ================================================================
# Shared, dependency-free home for handoff constants -- both ai_chat.py and
# workspace_agent_chat.py already import from this module, and
# workspace_agent_chat.py imports from ai_chat.py at top level, so routing
# shared state here (rather than through either agent module) avoids a
# circular import.

AGENT_DIRECTORY = {
    "gtm_strategist": {
        "name": "Charlie",
        "label": "GTM Strategist",
        "domain": "This specific assessment's scores, gaps, action items, roadmap, and evidence audits.",
    },
    "portfolio": {
        "name": "Nora",
        "label": "Portfolio Agent",
        "domain": "Score trends and comparisons across all of this workspace's assessments over time.",
    },
    "resource": {
        "name": "Theo",
        "label": "Resource Agent",
        "domain": "The workspace's resource library -- finding and recommending existing docs/tools/decks.",
    },
    "insights": {
        "name": "Milo",
        "label": "Insights Agent",
        "domain": "Recent workspace activity, score changes, pending reviews, overdue tasks, and documents.",
    },
}


def agent_display_label(agent_type: str) -> str:
    """"Name · Role" display string for UI (e.g. "Nora · Portfolio Agent") --
    the single source of truth so templates/JS don't hardcode names
    separately from AGENT_DIRECTORY."""
    info = AGENT_DIRECTORY.get(agent_type, {})
    name = info.get("name", agent_type)
    label = info.get("label", agent_type)
    return f"{name} · {label}" if name != label else label

# Hard cap on handoff chain length: origin agent + up to this many hops.
# Not model-facing -- a plain Python counter threaded through the tool
# builders. Without this, "full sub-conversation handoff" (each hop gets
# its own real tools, including further handoff tools) has no structural
# reason to terminate -- an LLM deciding call-by-call whether to hand off
# again has no built-in stopping condition.
MAX_HANDOFF_DEPTH = 2


def build_agent_directory_prompt(current_agent_type: str) -> str:
    """Render the other agents' labels/domains as a prompt paragraph telling
    `current_agent_type` who it can hand off to. Used to append onto every
    agent's system_instruction."""
    others = [
        f"- {info['name']} ({info['label']}): {info['domain']}"
        for agent_type, info in AGENT_DIRECTORY.items()
        if agent_type != current_agent_type
    ]
    return (
        "\n\nYou are one of several specialized GTM agents working together as a team:\n"
        + "\n".join(others)
        + "\n\nIf a question is better answered by one of these other agents, call the matching "
        "consult_ tool and weave its answer into your own response. Never say you can't "
        "collaborate with other agents -- you can, via these tools."
    )


def build_tagged_content_history(
    entries: Sequence[Any],
    max_turns: int = 16,
) -> List["types.Content"]:
    """Convert a chronologically-ordered, cross-agent timeline into a
    types.Content history list for client.chats.create(history=...).

    `entries` is a list of (speaker_label_or_None, message, response)
    tuples, already in chronological order (oldest first) and already
    filtered to exclude synthetic system rows (e.g. intent="insights_digest")
    -- this function only knows about the tuple shape, not model/intent
    semantics, so that filtering stays the caller's responsibility.

    `speaker_label` is prefixed onto the model turn when given (e.g.
    "[Resource Agent]:") -- Gemini's Content.role only distinguishes
    user/model, not which of several agents produced a given model turn, so
    without tagging, a shared multi-agent timeline is unattributable. Pass
    None for a row that belongs to the agent whose own history this is (no
    point prefixing an agent with its own name).

    Real conversational content, including handed-off exchanges
    (intent="handoff_query"), should NOT be excluded -- that's what gives a
    consulted agent (or, in Team mode, any agent) genuine memory of what
    already happened.
    """
    trimmed = list(entries)[-max_turns:]
    history: List[types.Content] = []
    for label, message, response in trimmed:
        message = (message or "").strip()
        response = (response or "").strip()
        if message:
            history.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))
        if response:
            text = f"[{label}]: {response}" if label else response
            history.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
    return history


def run_agent_turn(
    client,
    model: str,
    config,
    history: List["types.Content"],
    message: str,
    usage_label: str = "agent_chat",
) -> Optional[str]:
    """Run one conversational turn with real multi-turn memory via
    client.chats.create(history=...).send_message(message).

    Centralizes usage-tracking and the empty-response case (a turn that only
    produced function calls, no closing text). Returns None when there is no
    text so each call site can supply its own agent-appropriate fallback
    copy. Raises whatever the SDK raises on failure -- callers keep their own
    try/except + quota-cooldown handling, since the user-facing fallback
    message differs per agent.
    """
    chat = client.chats.create(model=model, config=config, history=history)
    response = chat.send_message(message)

    if MONITORING_AVAILABLE and getattr(response, "usage_metadata", None):
        AIUsageTracker.log_usage(response.usage_metadata.total_token_count, usage_label)

    if response.text is None or not response.text.strip():
        return None

    return response.text.strip()
