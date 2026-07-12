"""Workspace-scoped agent chat endpoints (portfolio, resource, insights).

Parallel to dashboard/views/agent_api.py's session-scoped GTM Agent chat,
kept in its own module rather than appended there, since these agents
reason over a whole Workspace instead of one AssessmentSession.
"""

import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.views.decorators.http import require_http_methods

from dashboard.utils_notifications import send_notification
from gtm.ai_services import _acquire_lock, _release_lock
from gtm.models import AssessmentSession, WorkspaceChatMessage
from gtm.models_workspace import WorkspaceMembership
from gtm.utils_async import run_in_background
from gtm.utils_logging import log_ai_error
from gtm.workspace_agent_chat import (
    AGENT_TYPES,
    build_insights_context,
    format_insights_digest_text,
    get_suggested_prompts_for_agent,
    process_workspace_chat_message,
)

from ..document_processors import _process_agent_attachments
from .helpers import _md, _parse_agent_request_payload, _resolve_dashboard_workspace

logger = logging.getLogger(__name__)

INSIGHTS_NOTIFY_ROLES = ("admin", "manager", "funti3r_consultant")


def _validate_agent_type(agent_type):
    if agent_type not in AGENT_TYPES:
        return JsonResponse({"success": False, "error": "Unknown agent type."}, status=400)
    return None


def _resolve_session_in_workspace(session_id, workspace):
    """Only trust a client-supplied session_id if it actually belongs to the
    resolved workspace, to avoid cross-workspace linkage or FK errors."""
    if not session_id:
        return None
    if AssessmentSession.objects.filter(uuid=session_id, workspace=workspace).exists():
        return session_id
    return None


def _resolve_team_session(request, session_id, current_workspace):
    """Team Chat's session check: trust session_id either when it belongs
    to the resolved workspace, or -- for a personal, no-workspace session --
    when the requester owns it directly."""
    if not session_id:
        return None
    if current_workspace and AssessmentSession.objects.filter(uuid=session_id, workspace=current_workspace).exists():
        return session_id
    if AssessmentSession.objects.filter(uuid=session_id, user=request.user, workspace__isnull=True).exists():
        return session_id
    return None


@require_http_methods(["POST"])
@login_required
def dashboard_workspace_agent_api(request, agent_type):
    """Run one of the workspace-scoped agents (portfolio/resource/insights)."""
    error = _validate_agent_type(agent_type)
    if error:
        return error

    current_workspace, _ = _resolve_dashboard_workspace(request)
    if not current_workspace:
        return JsonResponse({"success": False, "error": "Select a workspace first."}, status=400)

    session_id, message, file_type, uploaded_files, error_response = _parse_agent_request_payload(
        request, require_session=False
    )
    if error_response:
        return error_response

    resolved_session_id = _resolve_session_in_workspace(session_id, current_workspace)
    attachments, attachment_context, attachment_warnings = _process_agent_attachments(uploaded_files)

    result = process_workspace_chat_message(
        agent_type=agent_type,
        workspace_id=current_workspace.id,
        message=message,
        user=request.user,
        session_id=resolved_session_id,
        supplemental_context=attachment_context,
    )

    if result.get("success"):
        WorkspaceChatMessage.objects.create(
            workspace=current_workspace,
            session_id=resolved_session_id,
            agent_type=agent_type,
            user=request.user,
            message=message,
            response=result.get("response", ""),
            attachments=attachments,
            intent=result.get("intent", ""),
        )
        result["response_html"] = _md(result.get("response", ""))
        result["uploaded_attachments"] = attachments
        if attachment_warnings:
            result["attachment_warnings"] = attachment_warnings

    return JsonResponse(result, status=200 if result.get("success") else 500)


@require_http_methods(["POST"])
@login_required
def dashboard_workspace_agent_clear_api(request, agent_type):
    """Clear chat history for one workspace-scoped agent."""
    error = _validate_agent_type(agent_type)
    if error:
        return error

    current_workspace, _ = _resolve_dashboard_workspace(request)
    if not current_workspace:
        return JsonResponse({"success": False, "error": "Select a workspace first."}, status=400)

    deleted_count, _ = WorkspaceChatMessage.objects.filter(
        workspace=current_workspace, agent_type=agent_type
    ).delete()
    return JsonResponse({
        "success": True,
        "deleted_count": deleted_count,
        "message": "Chat history cleared.",
    })


@require_http_methods(["GET"])
@login_required
def dashboard_workspace_agent_context_api(request, agent_type):
    """Return suggested prompts + recent history for a workspace-scoped agent."""
    error = _validate_agent_type(agent_type)
    if error:
        return error

    current_workspace, _ = _resolve_dashboard_workspace(request)
    if not current_workspace:
        return JsonResponse({"success": False, "error": "Select a workspace first."}, status=400)

    prompts = get_suggested_prompts_for_agent(agent_type, current_workspace)

    recent_chats = WorkspaceChatMessage.objects.filter(
        workspace=current_workspace, agent_type=agent_type
    ).order_by('-created_at')[:40]

    history = []
    for chat in reversed(list(recent_chats)):
        history.append({
            'message': chat.message,
            'response': chat.response,
            'response_html': _md(chat.response or ''),
            'intent': chat.intent,
            'created_at': chat.created_at.isoformat(),
        })

    return JsonResponse({
        "success": True,
        "prompts": prompts,
        "history": history,
    })


# ================================================================
# TEAM CHAT -- one window, real transfer_to_agent between all 4 agents
# (gtm/team_chat.py). Persistence happens inside process_team_chat_message
# itself (into whichever agent's own table actually responded), so this
# view is a thin wrapper unlike the other chat endpoints above.
# ================================================================

@require_http_methods(["POST"])
@login_required
def dashboard_team_agent_api(request):
    """Run a Team Chat turn: resolves who's active, follows any transfer
    chain, and returns whichever agent ultimately answered."""
    from gtm.team_chat import process_team_chat_message

    current_workspace, _ = _resolve_dashboard_workspace(request)

    session_id, message, file_type, uploaded_files, error_response = _parse_agent_request_payload(
        request, require_session=False
    )
    if error_response:
        return error_response

    resolved_session_id = _resolve_team_session(request, session_id, current_workspace)

    if not current_workspace and not resolved_session_id:
        return JsonResponse({"success": False, "error": "Select a workspace or assessment first."}, status=400)

    attachments, attachment_context, attachment_warnings = _process_agent_attachments(uploaded_files)

    result = process_team_chat_message(
        workspace_id=current_workspace.id if current_workspace else None,
        message=message,
        user=request.user,
        session_id=resolved_session_id,
        supplemental_context=attachment_context,
        attachments=attachments,
    )

    if result.get("success"):
        result["response_html"] = _md(result.get("response", ""))
        result["uploaded_attachments"] = attachments
        if attachment_warnings:
            result["attachment_warnings"] = attachment_warnings

    return JsonResponse(result, status=200 if result.get("success") else 500)


# ================================================================
# INSIGHTS ON-DEMAND REFRESH (no scheduler — pull-based, mirrors the
# existing refresh_gap_suggestions/generate_gap_suggestions pattern)
# ================================================================

def _insights_status_cache_key(workspace_id) -> str:
    return f"gtm:agent:insights:{workspace_id}:status"


def _insights_lock_key(workspace_id) -> str:
    return f"gtm:ai:insights:{workspace_id}:lock"


def _generate_insights_digest_background(workspace_id, user_id):
    """Background job: compute the insights digest, persist it as a
    WorkspaceChatMessage, and notify admins/managers/consultants."""
    from gtm.models_workspace import Workspace

    status_key = _insights_status_cache_key(workspace_id)
    lock_key = _insights_lock_key(workspace_id)
    try:
        workspace = Workspace.objects.get(id=workspace_id)
        context = build_insights_context(workspace)
        digest_text = format_insights_digest_text(context)

        WorkspaceChatMessage.objects.create(
            workspace=workspace,
            agent_type="insights",
            user_id=user_id,
            message="[System] Refresh insights",
            response=digest_text,
            intent="insights_digest",
        )

        recipients = WorkspaceMembership.objects.filter(
            workspace=workspace, role__in=INSIGHTS_NOTIFY_ROLES, is_active=True
        ).select_related("user")
        for membership in recipients:
            send_notification(
                recipient=membership.user,
                title=f"New insights for {workspace.name}",
                message=digest_text[:200],
                notification_type="ai_report",
                level="info",
                workspace=workspace,
            )

        cache.set(status_key, "done", timeout=3600)
    except Exception as e:
        log_ai_error(
            "Workspace insights digest generation failed",
            e,
            service="google-genai",
            extra={"workspace_id": str(workspace_id)},
        )
        cache.set(status_key, "failed", timeout=600)
    finally:
        _release_lock(lock_key)


@require_http_methods(["POST"])
@login_required
def dashboard_agent_insights_refresh(request):
    """Kick off an insights digest refresh in the background (explicit trigger only)."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    if not current_workspace:
        return JsonResponse({"success": False, "error": "Select a workspace first."}, status=400)

    lock_key = _insights_lock_key(current_workspace.id)
    if not _acquire_lock(lock_key, ttl_seconds=180):
        return JsonResponse({"success": True, "status": "generating", "message": "Insights are already refreshing."})

    status_key = _insights_status_cache_key(current_workspace.id)
    cache.set(status_key, "generating", timeout=600)

    run_in_background(
        _generate_insights_digest_background,
        current_workspace.id,
        request.user.id,
        name=f"workspace_insights_digest:{current_workspace.id}",
    )

    return JsonResponse({"success": True, "status": "generating"})


@require_http_methods(["GET"])
@login_required
def dashboard_agent_insights_status(request):
    """HTMX/JSON polling endpoint for the insights digest refresh."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    if not current_workspace:
        return JsonResponse({"success": False, "error": "Select a workspace first."}, status=400)

    status_key = _insights_status_cache_key(current_workspace.id)
    status = cache.get(status_key)

    latest = WorkspaceChatMessage.objects.filter(
        workspace=current_workspace, agent_type="insights", intent="insights_digest"
    ).order_by('-created_at').first()

    if status is None:
        status = "done" if latest else "idle"

    return JsonResponse({
        "success": True,
        "status": status,
        "latest_digest": latest.response if latest else None,
        "latest_digest_html": _md(latest.response) if latest else None,
        "generated_at": latest.created_at.isoformat() if latest else None,
    })


# ================================================================
# AGENT DOCUMENTS -- generic list/edit/export for documents any of the 4
# agents can author (gtm/agent_documents.py). Creation and (chat-driven)
# editing happen via the create_document/edit_document tools; these views
# cover the Documents panel: listing, manual editing, and multi-format
# export. Scoped to either a workspace (the 3 workspace agent tabs) or a
# specific, ownership-checked session (the GTM Strategist tab).
# ================================================================

def _resolve_document_scope(request):
    """Resolve (workspace, session) for document endpoints. Prefers an
    explicit, ownership-checked session_id (GTM Strategist tab); falls back
    to the current workspace (the 3 workspace agent tabs)."""
    session_id = request.GET.get('session_id') or request.POST.get('session_id')
    if session_id:
        session = AssessmentSession.objects.filter(uuid=session_id).first()
        if session and (
            session.user_id == request.user.id
            or (session.workspace_id and WorkspaceMembership.objects.filter(
                workspace_id=session.workspace_id, user=request.user, is_active=True
            ).exists())
        ):
            return None, session

    current_workspace, _ = _resolve_dashboard_workspace(request)
    return current_workspace, None


def _get_scoped_document(pk, workspace, session):
    from gtm.models import AgentDocument

    qs = AgentDocument.objects.filter(pk=pk)
    if session is not None:
        qs = qs.filter(session=session)
    elif workspace is not None:
        qs = qs.filter(workspace=workspace)
    else:
        return None
    return qs.first()


@require_http_methods(["GET"])
@login_required
def document_list_api(request):
    """List documents in scope, for the sidebar Documents panel."""
    from gtm.agent_documents import list_agent_documents

    workspace, session = _resolve_document_scope(request)
    if not workspace and not session:
        return JsonResponse({"success": False, "error": "Select a workspace or assessment first."}, status=400)

    docs = list_agent_documents(workspace=workspace, session=session)[:50]
    return JsonResponse({
        "success": True,
        "documents": [
            {
                "id": str(d.pk),
                "title": d.title,
                "doc_type": d.doc_type,
                "doc_type_display": d.get_doc_type_display(),
                "agent_type": d.agent_type,
                "version": d.version,
                "updated_at": d.updated_at.isoformat(),
            }
            for d in docs
        ],
    })


@require_http_methods(["GET", "POST"])
@login_required
def document_edit_api(request, pk):
    """Fetch (GET) or manually edit (POST) a document's content -- mirrors
    the existing add_edit_resource pattern for a manual fallback alongside
    the chat-driven edit_document tool."""
    from gtm.agent_documents import edit_agent_document

    workspace, session = _resolve_document_scope(request)
    if not workspace and not session:
        return JsonResponse({"success": False, "error": "Select a workspace or assessment first."}, status=400)

    document = _get_scoped_document(pk, workspace, session)
    if not document:
        return JsonResponse({"success": False, "error": "Document not found."}, status=404)

    if request.method == "GET":
        return JsonResponse({
            "success": True,
            "id": str(document.pk),
            "title": document.title,
            "content": document.content,
            "doc_type": document.doc_type,
            "version": document.version,
        })

    new_content = (request.POST.get("content") or "").strip()
    if not new_content:
        return JsonResponse({"success": False, "error": "Content cannot be empty."}, status=400)

    updated = edit_agent_document(pk, new_content, workspace=workspace, session=session)
    if not updated:
        return JsonResponse({"success": False, "error": "Document not found."}, status=404)

    return JsonResponse({"success": True, "version": updated.version})


@require_http_methods(["GET"])
@login_required
def document_export(request, pk, fmt):
    """Export a document as PDF, Word, or Markdown, scoped to the
    requester's current workspace or a specific owned session."""
    from django.http import HttpResponse

    from gtm.utils_docx import render_insight_docx_response
    from gtm.utils_pdf import render_insight_pdf_response

    workspace, session = _resolve_document_scope(request)
    if not workspace and not session:
        return JsonResponse({"success": False, "error": "Select a workspace or assessment first."}, status=400)

    document = _get_scoped_document(pk, workspace, session)
    if not document:
        raise Http404("Document not found.")

    company_name = workspace.name if workspace else (session.company_name or "Company")

    if fmt == 'pdf':
        return render_insight_pdf_response(company_name=company_name, ai_playbook_md=document.content)
    elif fmt == 'docx':
        return render_insight_docx_response(company_name=company_name, ai_playbook_md=document.content)
    elif fmt in ('md', 'markdown'):
        resp = HttpResponse(document.content, content_type="text/markdown")
        resp["Content-Disposition"] = f'attachment; filename="{document.title}.md"'
        return resp
    elif fmt == 'txt':
        resp = HttpResponse(document.content, content_type="text/plain")
        resp["Content-Disposition"] = f'attachment; filename="{document.title}.txt"'
        return resp
    else:
        raise Http404("Unsupported export format.")
