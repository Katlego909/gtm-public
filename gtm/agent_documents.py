"""Shared service layer for agent-authored documents: create, edit, list,
read, search.

Used by gtm/ai_chat.py (the session-scoped GTM Strategist),
gtm/workspace_agent_chat.py (portfolio/resource/insights), and
gtm/action_item_completion.py (the one-shot task-completion agent), each
wrapping these as request-scoped closures bound to an already-authorized
session/workspace. This module itself only ever takes model instances
(never IDs the model could supply) except for `document_id` in
edit_agent_document/get_agent_document, which is the one place an
identifier is unavoidable -- both functions verify scope ownership before
allowing the edit/read.

Scoping convention: the GTM Strategist scopes to its own `session`
(narrower -- "documents about this assessment"); the three workspace
agents scope to `workspace` (broader -- every document in the workspace,
regardless of which agent or session created it, so e.g. the Portfolio
agent can see a document the GTM Strategist wrote). A document created by
the GTM Strategist is tagged with both `session` and `session.workspace`
(when present) so it's visible from either scope.
"""

from __future__ import annotations

from typing import Optional

from django.db import models

from .models import AgentDocument


def create_agent_document(
    *,
    agent_type: str,
    title: str,
    content: str,
    doc_type: str = "other",
    workspace=None,
    session=None,
    user=None,
) -> AgentDocument:
    """Create a new agent-authored document."""
    valid_doc_types = {choice[0] for choice in AgentDocument.DOC_TYPE_CHOICES}
    if doc_type not in valid_doc_types:
        doc_type = "other"
    return AgentDocument.objects.create(
        workspace=workspace,
        session=session,
        agent_type=agent_type,
        doc_type=doc_type,
        title=(title or "Untitled document").strip()[:200],
        content=content or "",
        created_by=user if user and getattr(user, "is_authenticated", False) else None,
    )


def _scope_queryset(workspace=None, session=None):
    """Session scope wins when both are given (narrower); falls back to
    workspace. Returns an empty queryset if neither is given -- there is no
    such thing as an unscoped document lookup."""
    if session is not None:
        return AgentDocument.objects.filter(session=session)
    if workspace is not None:
        return AgentDocument.objects.filter(workspace=workspace)
    return AgentDocument.objects.none()


def edit_agent_document(
    document_id,
    new_content: str,
    *,
    workspace=None,
    session=None,
) -> Optional[AgentDocument]:
    """Edit an existing document's content, bumping its version.

    Returns None if no document with that id exists in the given scope --
    this is the authorization check: a model-supplied document_id is never
    trusted without verifying it belongs to the caller's own scope first.
    """
    document = _scope_queryset(workspace=workspace, session=session).filter(pk=document_id).first()
    if not document:
        return None

    document.content = new_content or ""
    document.version += 1
    document.save(update_fields=["content", "version", "updated_at"])
    return document


def list_agent_documents(*, workspace=None, session=None, doc_type=None, exclude_doc_type=None):
    """List documents in scope, newest-updated first."""
    qs = _scope_queryset(workspace=workspace, session=session)
    if doc_type:
        qs = qs.filter(doc_type=doc_type)
    if exclude_doc_type:
        qs = qs.exclude(doc_type=exclude_doc_type)
    return qs


def get_agent_document(document_id, *, workspace=None, session=None) -> Optional[AgentDocument]:
    """Fetch one document's full content by id. Read-only sibling of
    edit_agent_document -- same scope authorization: a model-supplied
    document_id is never trusted without verifying it belongs to the
    caller's own scope first."""
    return _scope_queryset(workspace=workspace, session=session).filter(pk=document_id).first()


def search_agent_documents(query: str, *, workspace=None, session=None, doc_type=None):
    """Search documents in scope by title or content, newest-updated
    first. Same scoping as list_agent_documents."""
    qs = _scope_queryset(workspace=workspace, session=session)
    if doc_type:
        qs = qs.filter(doc_type=doc_type)
    if query:
        qs = qs.filter(models.Q(title__icontains=query) | models.Q(content__icontains=query))
    return qs


def _snippet_around(text: str, query: str, context: int = 100) -> str:
    """Return a short excerpt of `text` centered on the first
    case-insensitive match of `query`, so a search result tool can quote
    real evidence without dumping an entire document's text at the model.
    Falls back to the start of `text` if there's no match (e.g. the caller
    already filtered by a different field)."""
    text = text or ""
    if not query:
        return text[: context * 2].strip()
    idx = text.lower().find(query.lower())
    if idx == -1:
        return text[: context * 2].strip()
    start = max(0, idx - context)
    end = min(len(text), idx + len(query) + context)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet
