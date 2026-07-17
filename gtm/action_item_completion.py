"""On-demand AI completion of a single ActionItem.

Distinct from the chat agents in gtm/ai_chat.py / gtm/workspace_agent_chat.py
/ gtm/team_chat.py: this is not a conversation, it's one real, tool-using
agent turn scoped to a single ActionItem's task text. The point is to
actually perform the work the item describes -- ground it in this company's
real assessment data, produce the concrete deliverable the task calls for
(via the same create_document/edit_document/list_documents tools the chat
agents already use), and only then write back completion state.

Write-back is honest, not automatic: `status` only becomes "done" when a
real AgentDocument was actually produced this turn. If the agent judged
nothing needed drafting, the item moves to "doing" with a comment
explaining why -- not a false "done."
"""

import logging
import time
from typing import Any, Dict, List, Optional

from django.utils import timezone

from .agent_documents import create_agent_document, edit_agent_document, list_agent_documents
from .ai_chat import (
    _get_chat_client,
    _make_get_gtm_assessment_data_tool,
    _make_search_internal_resources_tool,
)
from .models import ActionItem, ActionItemComment, AgentDocument
from .utils_logging import log_ai_error

logger = logging.getLogger(__name__)

try:
    from google.genai import types
except ImportError:
    types = None

# Higher than the maximum_remote_calls=4 used for ordinary chat turns
# elsewhere in this app: a real completion turn already needs ~3 rounds
# before it can close (check assessment data, check existing documents,
# create the deliverable) -- 4 left no room for a final closing-text turn,
# so the response came back as a bare function_call with no summary.
MAX_TOOL_ROUNDS = 8


def _build_completion_tools(action_item: ActionItem, user, created_doc_ids: List[Any]) -> List[Any]:
    """Tool set for one completion turn: the same read tools the GTM
    Strategist has (assessment data, resource search -- so the agent checks
    what already exists rather than duplicating work) plus document tools
    that record the id of anything created/edited, so the caller can tell
    whether real work actually happened."""
    session = action_item.session
    get_gtm_assessment_data = _make_get_gtm_assessment_data_tool(session)
    search_internal_resources = _make_search_internal_resources_tool(session)

    def create_document(title: str, content: str, doc_type: str = "action_item_deliverable") -> str:
        """Create and save the concrete deliverable this task calls for
        (e.g. a policy, checklist, or standard) so it can be reviewed and
        exported as PDF, Word, or Markdown. `doc_type` should usually stay
        action_item_deliverable unless another type clearly fits better
        (client_summary, action_plan, roadmap, resource_brief, other).
        """
        doc = create_agent_document(
            agent_type="gtm_strategist",
            title=title,
            content=content,
            doc_type=doc_type,
            workspace=session.workspace,
            session=session,
            user=user,
        )
        created_doc_ids.append(doc.pk)
        return f'Saved "{doc.title}" (ID {doc.pk}).'

    def edit_document(document_id: str, new_content: str) -> str:
        """Edit an existing document's content by its ID (use list_documents
        first if you don't already know the ID) -- use this instead of
        create_document when a deliverable for this task already exists.
        """
        doc = edit_agent_document(document_id, new_content, session=session)
        if not doc:
            return "I couldn't find a document with that ID for this assessment."
        if doc.pk not in created_doc_ids:
            created_doc_ids.append(doc.pk)
        return f'Updated "{doc.title}" to version {doc.version}.'

    def list_documents(doc_type: str = "") -> str:
        """List documents already saved for this assessment, so you can
        check whether a deliverable for this task already exists before
        drafting a new one.
        """
        docs = list(list_agent_documents(session=session, doc_type=doc_type or None)[:10])
        if not docs:
            return "No documents have been saved for this assessment yet."
        lines = ["Documents for this assessment:"]
        for d in docs:
            lines.append(f"- [{d.pk}] {d.title} ({d.get_doc_type_display()}, v{d.version})")
        return "\n".join(lines)

    return [get_gtm_assessment_data, search_internal_resources, create_document, edit_document, list_documents]


def _build_completion_system_instruction(action_item: ActionItem) -> str:
    session = action_item.session
    company = session.company_name or "this company"
    return (
        f'You are completing one specific action item for {company}: "{action_item.note}"\n\n'
        "Actually complete this task -- do not just describe or plan it. Use your tools to ground the "
        "work in this company's real assessment data (scores, gaps, industry) and its existing resource "
        "library, then use create_document to produce the concrete, specific deliverable this task calls "
        "for (e.g. an actual SLA policy with real target numbers, an actual ICP one-pager with real "
        "inclusion/exclusion criteria, an actual checklist) -- not a generic template and not a "
        "description of what such a document would contain. Check list_documents first so you don't "
        "duplicate an existing deliverable; use edit_document instead if one already covers this task. "
        "When you're done, reply with a short (2-4 sentence) summary of exactly what you created and why "
        "it addresses the task."
    )


def complete_action_item(action_item: ActionItem, user=None) -> Dict[str, Any]:
    """Run one real, tool-using agent turn to actually perform the work an
    ActionItem describes, then write back the result. Returns a dict with
    `success`, and on success `status` ("done" or "doing"), `summary`, and
    `document_id` (None if no deliverable was produced)."""
    from .ai_services import (
        _extract_retry_delay_seconds,
        _is_quota_error,
        _quota_cooldown_active,
        _request_budget_available,
        _set_quota_cooldown,
    )
    from .ai_credits import resolve_account_for_session, can_spend, record_spend, format_reset_time

    session = action_item.session
    if session is None:
        return {"success": False, "error": "This action item has no linked assessment, so AI can't complete it yet."}

    account = resolve_account_for_session(session, user=user)

    if _quota_cooldown_active():
        return {"success": False, "error": "AI is cooling down to stay within API limits. Please try again shortly."}

    credit_check = can_spend(account=account)
    if not credit_check.allowed:
        reset_note = f" They reset at {format_reset_time(credit_check.reset_at)}." if credit_check.reset_at else ""
        return {"success": False, "error": "This workspace has used all of its AI credits for today." + reset_note}

    client = _get_chat_client()
    if not client:
        return {"success": False, "error": "Could not connect to the AI service. Please try again in a moment."}

    created_doc_ids: List[Any] = []
    tools = _build_completion_tools(action_item, user, created_doc_ids)
    config = types.GenerateContentConfig(
        system_instruction=_build_completion_system_instruction(action_item),
        temperature=0.6,
        max_output_tokens=2048,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        tools=tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=MAX_TOOL_ROUNDS),
    )

    start_time = time.monotonic()
    try:
        chat = client.chats.create(model="gemini-2.5-flash", config=config, history=[])
        response = chat.send_message(f"Complete this task: {action_item.note}")
        raw_text = (response.text or "").strip()
    except Exception as e:
        if _is_quota_error(e):
            _set_quota_cooldown(_extract_retry_delay_seconds(e))
            return {"success": False, "error": "Hit a temporary AI quota. Please try again shortly."}
        log_ai_error(
            "Action item AI completion failure",
            e,
            service="google-genai",
            model="gemini-2.5-flash",
            extra={"action_item_id": action_item.id},
        )
        return {"success": False, "error": "Ran into a technical error completing this task. Please try again."}

    duration_ms = round((time.monotonic() - start_time) * 1000)
    usage = getattr(response, "usage_metadata", None)
    prompt_token_count = getattr(usage, "prompt_token_count", None) if usage else None
    candidates_token_count = getattr(usage, "candidates_token_count", None) if usage else None
    if usage and getattr(usage, "total_token_count", None):
        record_spend(account, usage.total_token_count, "action_item_completion", session=session, actor=user)

    deliverable = None
    if created_doc_ids:
        deliverable = AgentDocument.objects.filter(pk__in=created_doc_ids).order_by("-updated_at").first()

    if raw_text:
        summary_text = raw_text
    elif deliverable:
        summary_text = f'Created "{deliverable.title}" to complete this task.'
    else:
        summary_text = "I looked into this task but didn't produce a deliverable."

    if deliverable:
        comment_text = f"AI completed this task: {summary_text}"
        action_item.deliverable_document = deliverable
        action_item.status = "done"
        action_item.completed_at = timezone.now()
        action_item.save(update_fields=["deliverable_document", "status", "completed_at", "updated_at"])
        result_status = "done"
    else:
        comment_text = f"AI attempted this task but didn't produce a deliverable: {summary_text}"
        action_item.status = "doing"
        action_item.save(update_fields=["status", "updated_at"])
        result_status = "doing"

    ActionItemComment.objects.create(
        action_item=action_item,
        user=user,
        text=comment_text,
        duration_ms=duration_ms,
        prompt_token_count=prompt_token_count,
        candidates_token_count=candidates_token_count,
    )

    return {
        "success": True,
        "status": result_status,
        "summary": summary_text,
        "document_id": str(deliverable.pk) if deliverable else None,
    }
