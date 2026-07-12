"""Workspace-scoped agent chat endpoints (portfolio, resource, insights).

Parallel to dashboard/views/agent_api.py's session-scoped GTM Agent chat,
kept in its own module rather than appended there, since these agents
reason over a whole Workspace instead of one AssessmentSession.
"""

import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_http_methods

from dashboard.utils_notifications import send_notification
from gtm.ai_services import _acquire_lock, _release_lock
from gtm.models import AssessmentSession, WorkspaceChatMessage
from gtm.models_workspace import WorkspaceMembership
from gtm.utils_async import run_in_background
from gtm.utils_logging import log_ai_error
from gtm.workspace_agent_chat import (
    AGENT_TYPES,
    CLIENT_SUMMARY_AGENT_TYPE,
    build_insights_context,
    draft_client_summary_text,
    format_insights_digest_text,
    get_suggested_prompts_for_agent,
    process_workspace_chat_message,
)

from ..parsers import log_workspace_activity
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

    result = process_workspace_chat_message(
        agent_type=agent_type,
        workspace_id=current_workspace.id,
        message=message,
        user=request.user,
        session_id=resolved_session_id,
    )

    if result.get("success"):
        WorkspaceChatMessage.objects.create(
            workspace=current_workspace,
            session_id=resolved_session_id,
            agent_type=agent_type,
            user=request.user,
            message=message,
            response=result.get("response", ""),
            intent=result.get("intent", ""),
        )
        result["response_html"] = _md(result.get("response", ""))

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
# CLIENT SUMMARY (client-facing deliverable) -- same on-demand refresh +
# poll pattern as the insights digest above, plus an export endpoint.
# Always persisted with agent_type="insights" regardless of which agent's
# tool triggered it, so exports don't need to know which agent generated it.
# CLIENT_SUMMARY_AGENT_TYPE is imported from gtm.workspace_agent_chat, the
# single source of truth both this module and the conversational tool use.
# ================================================================


def _client_summary_status_cache_key(workspace_id) -> str:
    return f"gtm:agent:client_summary:{workspace_id}:status"


def _client_summary_lock_key(workspace_id) -> str:
    return f"gtm:ai:client_summary:{workspace_id}:lock"


def _generate_client_summary_background(workspace_id, user_id):
    """Background job: draft a client summary, persist it, log activity, and notify."""
    from django.contrib.auth import get_user_model

    from gtm.models_workspace import Workspace

    status_key = _client_summary_status_cache_key(workspace_id)
    lock_key = _client_summary_lock_key(workspace_id)
    try:
        workspace = Workspace.objects.get(id=workspace_id)
        summary_text = draft_client_summary_text(workspace)

        chat_message = WorkspaceChatMessage.objects.create(
            workspace=workspace,
            agent_type=CLIENT_SUMMARY_AGENT_TYPE,
            user_id=user_id,
            message="[System] Generate client summary",
            response=summary_text,
            intent="client_summary",
        )

        User = get_user_model()
        actor = User.objects.filter(id=user_id).first() if user_id else None
        log_workspace_activity(
            workspace, actor, "client_summary_generated",
            f"Generated a client-facing summary ({len(summary_text)} chars)",
            object_type="workspace_chat_message", object_id=str(chat_message.pk),
        )

        recipients = WorkspaceMembership.objects.filter(
            workspace=workspace, role__in=INSIGHTS_NOTIFY_ROLES, is_active=True
        ).select_related("user")
        for membership in recipients:
            send_notification(
                recipient=membership.user,
                title=f"Client summary ready for {workspace.name}",
                message="A new client-ready summary has been drafted and is ready to review and export.",
                notification_type="ai_report",
                level="info",
                workspace=workspace,
            )

        cache.set(status_key, "done", timeout=3600)
    except Exception as e:
        log_ai_error(
            "Client summary generation failed",
            e,
            service="google-genai",
            extra={"workspace_id": str(workspace_id)},
        )
        cache.set(status_key, "failed", timeout=600)
    finally:
        _release_lock(lock_key)


@require_http_methods(["POST"])
@login_required
def dashboard_client_summary_refresh(request):
    """Kick off a client summary draft in the background (explicit trigger only)."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    if not current_workspace:
        return JsonResponse({"success": False, "error": "Select a workspace first."}, status=400)

    lock_key = _client_summary_lock_key(current_workspace.id)
    if not _acquire_lock(lock_key, ttl_seconds=180):
        return JsonResponse({"success": True, "status": "generating", "message": "A client summary is already being drafted."})

    status_key = _client_summary_status_cache_key(current_workspace.id)
    cache.set(status_key, "generating", timeout=600)

    run_in_background(
        _generate_client_summary_background,
        current_workspace.id,
        request.user.id,
        name=f"workspace_client_summary:{current_workspace.id}",
    )

    return JsonResponse({"success": True, "status": "generating"})


@require_http_methods(["GET"])
@login_required
def dashboard_client_summary_status(request):
    """HTMX/JSON polling endpoint for the client summary draft."""
    current_workspace, _ = _resolve_dashboard_workspace(request)
    if not current_workspace:
        return JsonResponse({"success": False, "error": "Select a workspace first."}, status=400)

    status_key = _client_summary_status_cache_key(current_workspace.id)
    status = cache.get(status_key)

    latest = WorkspaceChatMessage.objects.filter(
        workspace=current_workspace, agent_type=CLIENT_SUMMARY_AGENT_TYPE, intent="client_summary"
    ).order_by('-created_at').first()

    if status is None:
        status = "done" if latest else "idle"

    return JsonResponse({
        "success": True,
        "status": status,
        "latest_summary": latest.response if latest else None,
        "latest_summary_html": _md(latest.response) if latest else None,
        "latest_summary_id": latest.pk if latest else None,
        "generated_at": latest.created_at.isoformat() if latest else None,
    })


@require_http_methods(["GET"])
@login_required
def client_summary_export(request, pk, fmt):
    """Export a drafted client summary as PDF or Word, scoped to the
    requester's current workspace."""
    from gtm.utils_docx import render_insight_docx_response
    from gtm.utils_pdf import render_insight_pdf_response

    current_workspace, _ = _resolve_dashboard_workspace(request)
    if not current_workspace:
        return JsonResponse({"success": False, "error": "Select a workspace first."}, status=400)

    message = get_object_or_404(
        WorkspaceChatMessage, pk=pk, intent="client_summary", workspace=current_workspace
    )

    if fmt == 'pdf':
        return render_insight_pdf_response(company_name=message.workspace.name, ai_playbook_md=message.response)
    elif fmt == 'docx':
        return render_insight_docx_response(company_name=message.workspace.name, ai_playbook_md=message.response)
    else:
        raise Http404("Unsupported export format.")
