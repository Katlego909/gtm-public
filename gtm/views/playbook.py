# Split verbatim from the former monolithic gtm/views.py. Shared imports live
# in each module's header; shared helpers in gtm/views/helpers.py.
from ..utils_logging import log_error
from io import BytesIO
from django.shortcuts import render, redirect, get_object_or_404
from django.db.models import Count
from django.forms import Form, IntegerField
from django.forms.widgets import NumberInput
from django.db.models import Sum, F
from datetime import datetime
from django.utils import timezone
from datetime import timedelta
from django.contrib import messages
from ..models import AssessmentSession, Question, Response, Category, RecommendationBand, ActionItem, ToolRecommendation, ResultSnapshot, DeliveryDocument, CategoryDocument
from django.utils.safestring import mark_safe
import markdown as md
import math
import re
from django.http import HttpResponse, JsonResponse
from django.utils.html import strip_tags
from django.views.decorators.http import require_POST
from django.shortcuts import redirect
from django.utils.safestring import mark_safe
from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.contrib.auth import logout
from django.db.models import Avg
from django.conf import settings
from django.core.cache import cache
from functools import wraps
from ..utils import transfer_firmographics_to_snapshot, _client_id
from ..utils_async import run_in_background
from ..utils_pdf import render_gtm_report_pdf_response
from ..ai_services import (
    generate_playbook_with_gemini,
    generate_diagnostic_insight,
    generate_diagnostic_insights_batch,
    _normalize_ai_playbook_markdown,
    rewrite_context_note_with_ai,
    ENRICHMENT_UNAVAILABLE,
)
from ..forms import StartAssessmentForm # Added import
from ..services import (_expand_gtm_jargon, _build_question_guidance, _kickoff_playbook_generation, _log_access_denied, safe_get_session_or_403, _format_band_actions_markdown, _paginated_questions, _category_step_map, _first_incomplete_step, _compute_scores, _band_for_score, _is_session_complete, _save_snapshot)

from .helpers import (
    LEGEND,
    _get_template,
    _is_htmx,
    _remember_session,
    require_action_ownership,
    require_session_ownership,
)

def playbook_status(request, session_id):
    """Checks if AI playbook is ready. Returns button partials for polling."""
    # Access control: ensure user owns or is in session's workspace
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied to this session.")
    snap = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()

    if snap and (snap.ai_playbook or "").strip():
        # Playbook is ready! Return the actual button to view it.
        return render(request, "gtm/partials/playbook_ready_button.html", {"session": session})

    snap_status = getattr(snap, "ai_playbook_status", "pending") if snap else "pending"

    if snap_status == "failed":
        # Generation failed — stop polling
        return render(request, "gtm/partials/playbook_failed_button.html", {"session": session})

    if snap_status == "no_credits":
        # Out of AI credits for this period — stop polling, distinct from "failed"
        return render(request, "gtm/partials/playbook_limit_reached_button.html", {"session": session})

    if snap and snap_status == "pending":
        # Only kick if not already generating
        _kickoff_playbook_generation(snap, session_id=session.uuid)

    # Still generating. Return the loading state.
    return render(request, "gtm/partials/playbook_loading_button.html", {"session": session})


def playbook_footer_status(request, session_id):
    """HTMX polling endpoint for the footer 'View Playbook / Preparing' button on the results page."""
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied.")
    snap = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()
    ready = bool(snap and (snap.ai_playbook or "").strip())
    return render(request, "gtm/partials/playbook_footer_status.html", {
        "session": session,
        "ready": ready,
    })


def next_moves_content(request, session_id):
    """HTMX polling endpoint for the Recommended Next Moves section on the results page."""
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied.")

    snap = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()
    band = getattr(session, "band", None) or (snap.band if snap and snap.band_id else None)
    band_actions_html = _format_band_actions_markdown(getattr(band, "actions_markdown", "") if band else "")

    playbook_raw = (snap.ai_playbook or "").strip() if snap else ""
    is_generic_fallback = playbook_raw.startswith("No AI-generated playbook available yet.")
    snap_status = getattr(snap, "ai_playbook_status", "pending") if snap else "pending"

    ai_playbook_html = ""
    if snap and playbook_raw and not is_generic_fallback:
        src = _normalize_ai_playbook_markdown(snap.ai_playbook)
        ai_playbook_html = mark_safe(md.markdown(src, extensions=["extra", "sane_lists"]))

    from django.urls import reverse as _reverse
    poll_url = _reverse("gtm:next_moves_content", kwargs={"session_id": session.uuid})

    return render(request, "gtm/partials/next_moves_content.html", {
        "ai_playbook_html": ai_playbook_html,
        "band_actions_html": band_actions_html,
        "failed": snap_status == "failed" or is_generic_fallback,
        "limit_reached": snap_status == "no_credits",
        "poll_url": poll_url,
        "company_name": session.company_name or "your company",
    })


def enrichment_status(request, session_id):
    """HTMX polling endpoint: triggers and returns financial + competitor sections."""
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return HttpResponse("")

    snap = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()
    if not snap:
        return HttpResponse("")

    if not snap.ai_financial_summary or not snap.ai_competitor_analysis:
        # Debounce: HTMX polls this endpoint every few seconds. generate_enrichment_
        # sections holds its own lock, but this spawn guard stops us creating a fresh
        # thread on every poll while one is already in flight.
        spawn_guard = f"gtm:ai:enrichment:{snap.id}:spawn"
        if cache.add(spawn_guard, "1", timeout=120):
            from ..ai_services import generate_enrichment_sections

            def _run(snap_id):
                from ..models import ResultSnapshot as RS
                generate_enrichment_sections(RS.objects.get(id=snap_id))

            run_in_background(_run, snap.id, name="enrichment_sections")

    from ..ai_services import ENRICHMENT_NO_CREDITS

    fin_html = comp_html = ""
    fin_failed = (snap.ai_financial_summary == ENRICHMENT_UNAVAILABLE)
    comp_failed = (snap.ai_competitor_analysis == ENRICHMENT_UNAVAILABLE)
    fin_no_credits = (snap.ai_financial_summary == ENRICHMENT_NO_CREDITS)
    comp_no_credits = (snap.ai_competitor_analysis == ENRICHMENT_NO_CREDITS)

    if snap.ai_financial_summary and not fin_failed and not fin_no_credits:
        fin_html = mark_safe(md.markdown(_normalize_ai_playbook_markdown(snap.ai_financial_summary), extensions=["extra", "sane_lists"]))
    if snap.ai_competitor_analysis and not comp_failed and not comp_no_credits:
        comp_html = mark_safe(md.markdown(_normalize_ai_playbook_markdown(snap.ai_competitor_analysis), extensions=["extra", "sane_lists"]))

    # still_loading is True only while fields are genuinely empty (never attempted).
    # A sentinel value counts as "done" so the HTMX poll stops.
    still_loading = not (snap.ai_financial_summary and snap.ai_competitor_analysis)

    return render(request, "gtm/partials/enrichment_sections.html", {
        "session": session,
        "fin_html": fin_html,
        "comp_html": comp_html,
        "fin_failed": fin_failed,
        "comp_failed": comp_failed,
        "fin_no_credits": fin_no_credits,
        "comp_no_credits": comp_no_credits,
        "still_loading": still_loading,
    })


def playbook_content_status(request, session_id):
    """Return only the playbook content panel for incremental HTMX polling."""
    # Access control: ensure user owns or is in session's workspace
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied to this session.")
    snap = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()

    playbook_ready = bool(snap and (snap.ai_playbook or "").strip())
    ai_playbook_html = ""
    ai_financial_html = ""
    ai_competitor_html = ""

    if playbook_ready:
        src = _normalize_ai_playbook_markdown(snap.ai_playbook)
        ai_playbook_html = md.markdown(src, extensions=["extra", "sane_lists", "toc"])

        # Render financial summary if available
        if snap.ai_financial_summary:
            fin_src = _normalize_ai_playbook_markdown(snap.ai_financial_summary)
            ai_financial_html = md.markdown(fin_src, extensions=["extra", "sane_lists"])

        # Render competitor analysis if available
        if snap.ai_competitor_analysis:
            comp_src = _normalize_ai_playbook_markdown(snap.ai_competitor_analysis)
            ai_competitor_html = md.markdown(comp_src, extensions=["extra", "sane_lists"])

        return render(request, "gtm/partials/playbook_content_status.html", {
            "session": session,
            "playbook_ready": True,
            "playbook_loading": False,
            "ai_playbook_html": mark_safe(ai_playbook_html),
            "ai_financial_html": mark_safe(ai_financial_html),
            "ai_competitor_html": mark_safe(ai_competitor_html),
            "enrichment_loading": not (snap.ai_financial_summary and snap.ai_competitor_analysis),
        })

    snap_status = getattr(snap, "ai_playbook_status", "pending") if snap else "pending"

    return render(request, "gtm/partials/playbook_content_status.html", {
        "session": session,
        "playbook_ready": False,
        "playbook_loading": snap_status == "generating",
        "ai_playbook_html": "",
        "ai_financial_html": "",
        "ai_competitor_html": "",
        "snap_status": snap_status,
    })

# Playbook
def playbook(request, session_id):
    # Access control: ensure user owns or is in session's workspace
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied to this session.")
    
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)
    cat_sorted = sorted(cat_scores, key=lambda x: x["avg"])

    # -----------------------------
    # Format Recommended Next Moves (using centralized formatter)
    # -----------------------------
    band_actions_html = _format_band_actions_markdown(
        getattr(band, "actions_markdown", "") if band else ""
    )

    # Scoring engine context for company-specific next moves bullets
    from ..scoring_engine import build_recommendation_context
    _q_scores = {
        r.question.id_code: r.score
        for r in Response.objects.filter(session=session).select_related("question")
    }
    scoring_context = build_recommendation_context(_q_scores) if _q_scores else {}

    # Build short action bullets from pattern quick_wins (split into sentences)
    import re as _re
    _next_moves = []
    for _pat in scoring_context.get("patterns", []):
        _qw = (_pat.get("quick_win") or "").strip()
        _sentences = [s.strip() for s in _re.split(r'(?<=[.!?])\s+', _qw) if s.strip()]
        _next_moves.extend(_sentences[:2])
    next_move_bullets = _next_moves[:6]

    # Tool recommendations for the 2 weakest categories — flatten into bullet strings
    _weak_cat_ids = [c["category"].id for c in cat_sorted[:2]]
    _tool_recs = ToolRecommendation.objects.filter(category__id__in=_weak_cat_ids).select_related("category")[:5]
    playbook_tool_recs = [
        f"{_tr.tools} — {_tr.description}".strip(" —") if _tr.description else _tr.tools
        for _tr in _tool_recs if _tr.tools
    ]

    # Auto-populate task list on first visit (no tasks yet)
    # 90% from AI playbook via Gemini extraction, 10% from scoring engine quick_win
    if scoring_context and not session.actions.exists():
        from ..ai_services import extract_tasks_from_playbook
        _snap_for_tasks = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()
        _playbook_text = (_snap_for_tasks.ai_playbook or "").strip() if _snap_for_tasks else ""
        _company = session.company_name or "your company"
        _patterns = scoring_context.get("patterns", [])
        _severity_days = {"Critical": 14, "High": 28, "Medium": 42}
        _tasks_to_create = []

        # Try 90%: extract ~4 tasks from AI playbook
        from ..ai_credits import resolve_account_for_session
        _ai_tasks = extract_tasks_from_playbook(
            _playbook_text, _company, n=4, account=resolve_account_for_session(session)
        ) if _playbook_text else []

        if _ai_tasks:
            _default_due = timezone.now().date() + timedelta(days=21)
            for _t in _ai_tasks:
                _tasks_to_create.append(ActionItem(session=session, note=_t, status="todo", due_date=_default_due))
            # 10%: first quick_win sentence from highest-severity pattern
            if _patterns:
                _top_pat = _patterns[0]
                _qw = (_top_pat.get("quick_win") or "").strip()
                _first = _re.split(r'(?<=[.!?])\s+', _qw)[0].strip() if _qw else ""
                _due = timezone.now().date() + timedelta(days=_severity_days.get(_top_pat["severity"], 28))
                _first_words = set(_first.lower().split()) if _first else set()
                _too_similar = any(
                    len(_first_words & set(_t.lower().split())) / max(len(_first_words), 1) > 0.6
                    for _t in _ai_tasks
                )
                if _first and len(_first) <= 240 and not _too_similar:
                    _tasks_to_create.append(ActionItem(session=session, note=_first, status="todo", due_date=_due))
        else:
            # Fallback: all scoring engine quick_win sentences
            for _pat in _patterns:
                _due = timezone.now().date() + timedelta(days=_severity_days.get(_pat["severity"], 28))
                _qw = (_pat.get("quick_win") or "").strip()
                for _s in [s.strip() for s in _re.split(r'(?<=[.!?])\s+', _qw) if s.strip()]:
                    if len(_s) <= 240:
                        _tasks_to_create.append(ActionItem(session=session, note=_s, status="todo", due_date=_due))

        ActionItem.objects.bulk_create(_tasks_to_create)

    # -----------------------------
    # AI Playbook Rendering (+ optional lazy-generate)
    # -----------------------------
    # Ensure we actually have a snapshot even if user skips Results page
    snap = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()

    # (Optional) Generate if empty - NON-BLOCKING to prevent page hangs
    needs_generation = snap and not (snap.ai_playbook or "").strip()
    
    # Trigger background generation if needed (non-blocking)
    if needs_generation:
        _kickoff_playbook_generation(snap, session_id=session.uuid)
    
    playbook_ready = False
    playbook_loading = bool(needs_generation)
    ai_playbook_html = ""
    ai_financial_html = ""
    ai_competitor_html = ""
    try:
        if snap and getattr(snap, "ai_playbook", ""):
            src = _normalize_ai_playbook_markdown(snap.ai_playbook)
            ai_playbook_html = md.markdown(
                src,
                extensions=["extra", "sane_lists", "toc"]  # 'extra' already includes tables
            )

            # Render financial summary if available
            if snap.ai_financial_summary:
                fin_src = _normalize_ai_playbook_markdown(snap.ai_financial_summary)
                ai_financial_html = md.markdown(fin_src, extensions=["extra", "sane_lists"])

            # Render competitor analysis if available
            if snap.ai_competitor_analysis:
                comp_src = _normalize_ai_playbook_markdown(snap.ai_competitor_analysis)
                ai_competitor_html = md.markdown(comp_src, extensions=["extra", "sane_lists"])

            playbook_ready = True
    except Exception as e:
        log_error("AI Playbook rendering", e, {"session_id": str(session.uuid)})
        ai_playbook_html = ""
        ai_financial_html = ""
        ai_competitor_html = ""
        playbook_ready = False
        playbook_loading = False

    # Note: Action items are now created explicitly via the "build my action plan" agent command.
    # This prevents duplicate auto-creation and gives users explicit control over task generation.

    # Render Template
    # ----
    return render(request, "gtm/playbook.html", {
        "session": session,
        "overall": round(overall, 1),
        "band": band,
        "cat_scores": cat_scores,
        "cat_sorted": cat_sorted,
        "band_actions_html": band_actions_html,
        "scoring_context": scoring_context,
        "next_move_bullets": next_move_bullets,
        "playbook_tool_recs": playbook_tool_recs,
        "ai_playbook_html": mark_safe(ai_playbook_html),
        "ai_financial_html": mark_safe(ai_financial_html),
        "ai_competitor_html": mark_safe(ai_competitor_html),
        "playbook_ready": playbook_ready,
        "playbook_loading": playbook_loading,
        "is_htmx": _is_htmx(request),
    })


def insight_status(request, session_id, response_id):
    """Return only one insight block so the results page updates without full-page refresh."""
    # Access control: ensure user owns or is in session's workspace
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied to this session.")

    response = get_object_or_404(Response.objects.select_related("question"), pk=response_id, session=session)

    # Refresh both fields from DB to check current status
    response.refresh_from_db(fields=["ai_insight", "ai_insight_status"])

    if not (response.ai_insight or "").strip():
        status = getattr(response, 'ai_insight_status', 'pending')

        if status == "pending":
            from ..ai_credits import resolve_account_for_session, can_spend
            if not can_spend(account=resolve_account_for_session(session)).allowed:
                response.ai_insight_status = "no_credits"
                response.save(update_fields=["ai_insight_status"])
            else:
                # Only spawn a thread if generation hasn't started yet
                def gen_diagnostic_async(resp_id):
                    from ..models import Response
                    from ..ai_services import generate_diagnostic_insight
                    r = Response.objects.select_related("question", "session__snapshot").get(id=resp_id)
                    generate_diagnostic_insight(r)

                run_in_background(gen_diagnostic_async, response.id, name="diagnostic_insight_poll")
        # If status is "generating", "failed", or "no_credits", don't spawn another thread

    return render(request, "gtm/partials/insight_status.html", {
        "session": session,
        "response": response,
    })


def download_report_pdf(request, session_id):
    # Access control: ensure user owns or is in session's workspace
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied to this session.")
    
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)
    return render_gtm_report_pdf_response(session=session, cat_scores=cat_scores, overall=overall, band=band)

