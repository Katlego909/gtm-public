# Split verbatim from the former monolithic gtm/views.py. Shared imports live
# in each module's header; shared helpers in gtm/views/helpers.py.
from ..utils_logging import log_error
from collections import defaultdict
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
from ..services import (_expand_gtm_jargon, _build_question_guidance, _kickoff_playbook_generation, _log_access_denied, safe_get_session_or_403, _format_band_actions_markdown, _paginated_questions, _category_step_map, _first_incomplete_step, _compute_scores, _band_for_score, _is_session_complete, _save_snapshot, _applicable_categories)

from .helpers import (
    LEGEND,
    _get_template,
    _is_htmx,
    _remember_session,
    require_action_ownership,
    require_session_ownership,
)

# Performance Breakdown bar/badge colors, keyed by absolute pillar-score
# severity (not relative ranking -- see _pillar_severity). Validated with the
# dataviz skill's palette checker (CVD separation + normal-vision floor both
# pass; the "warning" tier's lower surface contrast is an accepted tradeoff
# from the skill's own reference palette, mitigated by always pairing it with
# a text badge rather than color alone).
_SEVERITY_BAR_COLOR = {"critical": "#d03b3b", "warning": "#fab219", "good": "#0ca30c"}
_SEVERITY_BADGE_CLASS = {
    "critical": "bg-red-50 text-red-700",
    "warning": "bg-amber-50 text-amber-700",
    "good": "bg-green-50 text-green-700",
}
_SEVERITY_LABEL = {"critical": "Needs Attention", "warning": "On Track", "good": "Strong"}


def _pillar_severity(avg):
    """Weak/mid/strong bands for a 0-5 pillar score, matching the 2.5/3.5
    cutoffs scoring_engine.py's EARLY_STAGE_ALL_LOW/SCALING_READY patterns
    already use to describe pillar health."""
    if avg < 2.5:
        return "critical"
    if avg < 3.5:
        return "warning"
    return "good"


def results(request, session_id):
    # Access control: ensure user owns or is in session's workspace
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied to this session.")
    
    # Ensure workspace association if missing
    if hasattr(request, 'workspace') and request.workspace and not session.workspace:
        session.workspace = request.workspace
        session.save(update_fields=["workspace"])
    
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)

    # Mark pillars not applicable to this session's company_stage so the
    # template can show "not yet applicable" instead of a misleading 0/5,
    # and so they don't pollute strengths/focus-area ranking below.
    applicable_names = set(_applicable_categories(session).values_list("name", flat=True))
    for c in cat_scores:
        c["is_applicable"] = c["category"].name in applicable_names
        if c["is_applicable"]:
            severity = _pillar_severity(c["avg"])
            c["bar_color"] = _SEVERITY_BAR_COLOR[severity]
            c["badge_class"] = _SEVERITY_BADGE_CLASS[severity]
            c["badge_label"] = _SEVERITY_LABEL[severity]
    exempted_names = [c["category"].name for c in cat_scores if not c["is_applicable"]]
    stage_exemption_note = ""
    if exempted_names:
        if len(exempted_names) == 1:
            joined = exempted_names[0]
        else:
            joined = ", ".join(exempted_names[:-1]) + f" & {exempted_names[-1]}"
        verb = "isn't" if len(exempted_names) == 1 else "aren't"
        stage_exemption_note = (
            f"{joined} {verb} scored yet at your company's current stage — "
            "they'll unlock as you grow."
        )

    scoreable_cats = [c for c in cat_scores if c["is_applicable"]]
    step_map = {cat.id: idx + 1 for idx, cat in enumerate(Category.objects.all().order_by("id"))}
    strengths_categories = sorted(scoreable_cats, key=lambda x: x["avg"], reverse=True)[:3]
    focus_categories = sorted(scoreable_cats, key=lambda x: x["avg"])[:3]

    focus_category_ids = {c["category"].id for c in focus_categories}
    for c in cat_scores:
        c["is_focus"] = c["category"].id in focus_category_ids

    for it in strengths_categories:
        it["step"] = step_map.get(it["category"].id)
    for it in focus_categories:
        it["step"] = step_map.get(it["category"].id)

    all_rows = []
    questions = list(Question.objects.select_related("category").all())
    responses_by_question_id = {
        r.question_id: r
        for r in Response.objects.filter(
            session=session,
            question__in=questions,
        ).select_related("question__category")
    }

    for q in questions:
        r = responses_by_question_id.get(q.id)
        if r:
            all_rows.append({
                "question": q,
                "response_id": r.id,
                "score": r.score,
                "weighted": r.score * q.weight,
                "note": q.diagnostic_note or "",
                "step": step_map.get(q.category_id),
                # Include the AI insight from the Response object
                "ai_insight": r.ai_insight or "",
            })
    # One weakest question per category, not a pure global bottom-3: without
    # this, Question.objects.all() (no order_by, so PK order) makes sorted()'s
    # stable tie-break always favor whichever category was seeded with the
    # lowest PKs (Demand) on every weighted-score tie -- silently starving
    # other pillars out of this panel even when their scores are just as weak.
    rows_by_category = defaultdict(list)
    for row in all_rows:
        rows_by_category[row["question"].category_id].append(row)
    weakest_per_category = [min(rows, key=lambda x: x["weighted"]) for rows in rows_by_category.values()]
    weakest_questions = sorted(weakest_per_category, key=lambda x: x["weighted"])[:3]

    labels = [c["category"].name for c in cat_scores]
    values = [round(c["avg"], 2) for c in cat_scores]

    # ── Scoring engine: opportunity ranking + pattern detection ──────────────
    from ..scoring_engine import build_recommendation_context
    _q_scores = {
        r.question.id_code: r.score
        for r in Response.objects.filter(session=session).select_related("question")
    }
    scoring_context = build_recommendation_context(_q_scores) if _q_scores else {}

    # -----------------------------
    # Tool Recommendations Logic (Optimized to prevent N+1)
    # -----------------------------
    recommendations = []
    
    # 1. Collect unique categories from weakest_questions
    weakest_category_ids = set()
    for w in weakest_questions:
        weakest_category_ids.add(w["question"].category.id)

    # 2. Fetch all ToolRecommendation objects for these categories in one query
    #    Prefetch the category to avoid N+1 when accessing m.category.id later if needed
    all_tool_recommendations = ToolRecommendation.objects.filter(
        category__id__in=list(weakest_category_ids)
    ).select_related('category') # select_related for accessing category name later efficiently

    # 3. Perform keyword matching in Python
    tools_by_category = {}
    for tool in all_tool_recommendations:
        tools_by_category.setdefault(tool.category_id, []).append(tool)

    for w in weakest_questions:
        q_text = (w["question"].text or "").lower()
        q_note = (w.get("note") or "").lower()
        
        # Filter through prefetched recommendations for the current question's category only.
        for m in tools_by_category.get(w["question"].category_id, []):
            kw = (m.keyword or "").lower()
            if kw and (kw in q_text or kw in q_note):
                recommendations.append(m)

    seen = set()
    uniq = []
    for r in recommendations:
        if r.id not in seen:
            seen.add(r.id)
            uniq.append(r)
    recommendations = uniq[:6]
    
    # FALLBACK: If insufficient recommendations, add generic tools from weakest categories
    if len(recommendations) < 5:
        # Get unique weakest categories not already represented
        weak_categories = []
        for w in weakest_questions:
            cat = w["question"].category
            if cat not in weak_categories:
                weak_categories.append(cat)
        
        # Add generic tools from each weak category until we have at least 5
        for cat in weak_categories:
            if len(recommendations) >= 5:
                break

            # Reuse prefetched tools for fallback; avoid per-category DB queries.
            for tool in tools_by_category.get(cat.id, []):
                if tool.id not in seen:
                    seen.add(tool.id)
                    recommendations.append(tool)
                    if len(recommendations) >= 5:
                        break

    # -----------------------------
    # Render band actions (using centralized formatter)
    # -----------------------------
    band_actions_html = _format_band_actions_markdown(
        getattr(band, "actions_markdown", "") if band else ""
    )

    # -----------------------------
    # Save snapshot (single source of truth)
    # -----------------------------
    # Let _save_snapshot handle update_or_create and return the instance
    snap = _save_snapshot(session, cat_scores, overall, band, labels, values)

    # -----------------------------
    # Generate AI insights for low-scoring questions (if not already generated)
    # -----------------------------
    # Prefetch all relevant responses for weakest questions to avoid N+1
    weakest_question_ids = [q_data["question"].id for q_data in weakest_questions]
    
    # Fetch responses and map question_id to response manually as question_id is not unique for in_bulk()
    responses_qs = Response.objects.filter(
        session=session,
        question__id__in=weakest_question_ids
    ).select_related('question') # select_related to avoid N+1 when accessing response.question later
    
    responses_by_question_id = {r.question.id: r for r in responses_qs}

    # Collect responses needing batch AI insight generation
    responses_needing_insight = []
    for q_data in weakest_questions:
        response = responses_by_question_id.get(q_data["question"].id)
        if response:
            if not (response.ai_insight or "").strip() and response.ai_insight_status == "pending":
                responses_needing_insight.append(response)
                q_data["ai_insight_loading"] = True
            else:
                q_data["ai_insight"] = response.ai_insight

    # Resolved once and reused for both the batch-insight check below and
    # the playbook kickoff further down -- avoids a duplicate
    # get_or_create query for the same session's credit account.
    from ..ai_credits import resolve_account_for_session, can_spend
    ai_credit_account = resolve_account_for_session(session)

    # TRIGGER BATCH ASYNC GENERATION (one thread for all insights, not one per response)
    if responses_needing_insight:
        resp_ids_for_batch = [r.id for r in responses_needing_insight]

        if not can_spend(account=ai_credit_account).allowed:
            Response.objects.filter(id__in=resp_ids_for_batch).update(ai_insight_status="no_credits")
            for q_data in weakest_questions:
                if q_data.get("ai_insight_loading"):
                    q_data["ai_insight_loading"] = False
        else:
            def gen_batch_async(r_ids):
                from ..models import Response as ResponseModel
                fresh_responses = list(
                    ResponseModel.objects.filter(id__in=r_ids).select_related("question", "session__snapshot")
                )
                generate_diagnostic_insights_batch(fresh_responses)

            run_in_background(gen_batch_async, resp_ids_for_batch, name="batch_diagnostic_insights")

    
    # -----------------------------
    # Trigger AI playbook generation in background (non-blocking)
    # -----------------------------
    playbook_raw = (snap.ai_playbook or "").strip() if snap else ""
    # The final fallback stored when Gemini fails — treat as if no playbook exists
    is_generic_fallback = playbook_raw.startswith("No AI-generated playbook available yet.")

    if snap and (not playbook_raw or is_generic_fallback):
        if is_generic_fallback:
            snap.ai_playbook = ""
            snap.ai_playbook_status = "pending"
            snap.save(update_fields=["ai_playbook", "ai_playbook_status"])
        _kickoff_playbook_generation(snap, session_id=session.uuid, account=ai_credit_account)

    # Render AI playbook HTML for the "Recommended Next Moves" card
    ai_playbook_html = ""
    if snap and playbook_raw and not is_generic_fallback:
        src = _normalize_ai_playbook_markdown(snap.ai_playbook)
        ai_playbook_html = mark_safe(md.markdown(src, extensions=["extra", "sane_lists"]))

    from django.urls import reverse as _reverse
    next_moves_poll_url = _reverse("gtm:next_moves_content", kwargs={"session_id": session.uuid})
    snap_status = getattr(snap, "ai_playbook_status", "pending") if snap else "pending"
    total_steps = len(_paginated_questions(session))

    return render(request, "gtm/results.html", {
        "session": session,
        "total_steps": total_steps,
        "overall": round(overall, 1),
        "band": band,
        "band_actions_html": band_actions_html,
        "ai_playbook_html": ai_playbook_html,
        "cat_scores": cat_scores,
        "labels": labels,
        "values": values,
        "strengths_categories": strengths_categories,
        "focus_categories": focus_categories,
        "weakest_questions": weakest_questions,
        "recommendations": recommendations,
        "scoring_context": scoring_context,
        "stage_exemption_note": stage_exemption_note,
        "is_htmx": _is_htmx(request),
        "snap": snap,
        "next_moves_poll_url": next_moves_poll_url,
        "next_moves_failed": snap_status == "failed" or is_generic_fallback,
    })

