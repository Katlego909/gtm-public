"""Shared mechanical helpers for running a Gemini chat turn with real
multi-turn memory. Houses only the pieces that are byte-for-byte identical
across gtm/ai_chat.py and gtm/workspace_agent_chat.py -- system prompts, tool
lists, and quota-error handling stay local to each agent module since they
already diverge per agent.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Sequence

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


def record_task_ref(
    sink: Optional[List[Dict[str, Any]]],
    task_id: Any,
    note: str,
    cap: int = 20,
) -> None:
    """Record that a tool call surfaced a real ActionItem id this turn, so
    it can be persisted structurally (see WorkspaceChatMessage/ChatMessage's
    task_refs field) instead of only existing as a `[ID: n]` substring in
    text the model may or may not repeat back to the user.

    `sink` is a plain mutable list threaded into tool closures by the
    caller (same idiom as gtm/action_item_completion.py's `created_doc_ids`
    list) -- a no-op when `sink is None`, so every call site can call this
    unconditionally instead of guarding it. Dedupes by id: a repeat id is
    moved to the end (last-seen wins) rather than duplicated, and the sink
    is capped at `cap` entries by evicting the oldest once it grows past
    that, so a long tool-calling turn can't grow this unboundedly.
    """
    if sink is None:
        return
    for i, ref in enumerate(sink):
        if ref["id"] == task_id:
            del sink[i]
            break
    sink.append({"id": task_id, "note": (note or "")[:80]})
    del sink[:-cap]


def record_document_ref(
    sink: Optional[List[Dict[str, Any]]],
    document_or_ref: Any,
    action: str = "created",
    cap: int = 10,
) -> None:
    """Record that a tool call created/edited a real AgentDocument this
    turn, so it can be persisted structurally (see ChatMessage/
    WorkspaceChatMessage's document_refs field) and rendered as an inline
    card in the chat bubble instead of only surfacing later in the sidebar
    Documents panel.

    Accepts either a real AgentDocument instance (the normal case, called
    right after create_agent_document/edit_agent_document returns -- pass
    `action="created"`/`"updated"` accordingly) or an already-built ref
    dict (bubbling a nested turn's document_refs up through a
    consult/handoff boundary, same idiom record_task_ref uses for task
    refs). `sink` is a no-op when None. Dedupes by id (repeat id moves to
    the end, last write wins) and caps at `cap` entries.

    Deliberately does NOT feed into build_task_context_prompt/
    system_instruction the way record_task_ref does -- document_refs
    exists purely for client-side display, not for the model's own
    turn-to-turn grounding (the model already got the doc's id/title back
    as its own tool-call result text).
    """
    if sink is None:
        return
    if isinstance(document_or_ref, dict):
        ref = dict(document_or_ref)
    else:
        doc = document_or_ref
        ref = {
            "id": str(doc.pk),
            "title": (doc.title or "Untitled document")[:200],
            "doc_type": doc.doc_type,
            "doc_type_display": doc.get_doc_type_display(),
            "version": doc.version,
            "action": action,
        }
    for i, existing in enumerate(sink):
        if existing["id"] == ref["id"]:
            del sink[i]
            break
    sink.append(ref)
    del sink[:-cap]


def build_task_context_prompt(task_refs: Optional[List[Dict[str, Any]]] = None) -> str:
    """Shared instruction appended to every agent's system prompt (workspace
    agents, Charlie, and Team mode) covering what a task-reference chain
    needs: today's date, so a relative reference like "the task from July
    16th" resolves to the right year when passed to find_tasks' created_on
    parameter, and real task IDs the conversation has already surfaced --
    kept OUT of the visible chat bubble by design (the user chose not to
    show raw IDs), so they're threaded back in here via system_instruction
    rather than via conversation history text. Never append this kind of
    text onto a *historical model turn* instead -- Team mode already hit
    that exact failure mode once (see _build_team_history's docstring on
    the self-tagging leak) where text appended to an agent's own past
    turns taught it to keep re-emitting that text in new replies.
    """
    from django.utils import timezone

    today = timezone.localdate().strftime("%B %d, %Y")
    prompt = (
        f"\n\nToday's date is {today}. "
        "When you look up tasks (e.g. via find_tasks, get_overdue_tasks, or "
        "review_current_action_items), you'll get back real IDs like '[ID: 42]' -- "
        "these are for your own use only. Never show a literal ID or technical "
        "identifier to the user; refer to tasks naturally by their description "
        "instead (e.g. 'the discovery questions task'). The system tracks the real "
        "IDs for you automatically behind the scenes, so you don't need to repeat "
        "one back in your reply for it to be remembered -- just act on it or "
        "describe the task in plain language."
    )
    if task_refs:
        refs_text = "; ".join(f"[ID: {ref['id']}] {ref['note']}" for ref in task_refs)
        prompt += (
            "\n\nTasks already surfaced earlier in this conversation (system context "
            f"only -- never repeat these IDs verbatim to the user): {refs_text}"
        )
    return prompt


_TASK_ID_BRACKET_RE = re.compile(r"\s*\[ID:\s*\d+\]", re.IGNORECASE)


def strip_task_id_brackets(text: Optional[str]) -> Optional[str]:
    """Defensive regex failsafe: removes any literal '[ID: n]' marker a
    model wrote into its own reply despite being told not to (see
    build_task_context_prompt), so a leaked technical ID never reaches the
    user even if the instruction is ignored. Mirrors the existing
    _strip_imperative_sentences failsafe in gtm/ai_services.py, which
    removes recommendation-style sentences from diagnostic insights for the
    same reason: prompting alone isn't a hard guarantee."""
    if not text:
        return text
    cleaned = _TASK_ID_BRACKET_RE.sub("", text)
    return re.sub(r" {2,}", " ", cleaned).strip()


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
    account=None,
    session=None,
    feature: str = None,
) -> Optional[str]:
    """Run one conversational turn with real multi-turn memory via
    client.chats.create(history=...).send_message(message).

    Centralizes usage-tracking (both the global observational AIUsageTracker
    counter, keyed by `usage_label`, and, when `account` is passed, the
    enforced per-workspace/user AI credit ledger -- see gtm/ai_credits.py,
    keyed by `feature`, which is a stricter/coarser AICreditTransaction
    category and defaults to `usage_label` when not given separately) and
    the empty-response case (a turn that only produced function calls, no
    closing text). Returns None when there is no text so each call site can
    supply its own agent-appropriate fallback copy. Raises whatever the SDK
    raises on failure -- callers keep their own try/except + quota-cooldown
    handling, since the user-facing fallback message differs per agent.
    """
    chat = client.chats.create(model=model, config=config, history=history)
    response = chat.send_message(message)

    if getattr(response, "usage_metadata", None):
        total_tokens = response.usage_metadata.total_token_count
        if MONITORING_AVAILABLE:
            AIUsageTracker.log_usage(total_tokens, usage_label)
        if account is not None:
            from .ai_credits import record_spend
            record_spend(account, total_tokens, feature or usage_label, session=session)

    if response.text is None or not response.text.strip():
        return None

    return strip_task_id_brackets(response.text.strip())
