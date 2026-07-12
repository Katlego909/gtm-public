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


def build_content_history(rows: Sequence[Any], max_turns: int = 12) -> List["types.Content"]:
    """Convert chronologically-ordered chat rows into a types.Content history
    list for client.chats.create(history=...).

    `rows` must already be in chronological order (oldest first) and already
    filtered to exclude synthetic system rows (e.g. intent="insights_digest"
    or "client_summary") -- this function only knows about `.message`/
    `.response` fields, not intent semantics, so that filtering stays the
    caller's responsibility.
    """
    trimmed = list(rows)[-max_turns:]
    history: List[types.Content] = []
    for row in trimmed:
        message = (row.message or "").strip()
        response = (row.response or "").strip()
        if message:
            history.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))
        if response:
            history.append(types.Content(role="model", parts=[types.Part.from_text(text=response)]))
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
