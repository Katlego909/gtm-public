from .utils_logging import log_error
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
from .models import AssessmentSession, Question, Response, Category, RecommendationBand, ActionItem, ToolRecommendation, ResultSnapshot, DeliveryDocument, CategoryDocument
from django.utils.safestring import mark_safe
import markdown as md
import math
import re
from django.http import HttpResponse, JsonResponse
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.units import cm
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
from .utils import transfer_firmographics_to_snapshot, _client_id
from .utils_async import run_in_background
from .utils_pdf import render_gtm_report_pdf_response
from .ai_services import (
    generate_playbook_with_gemini,
    generate_diagnostic_insight,
    generate_diagnostic_insights_batch,
    _normalize_ai_playbook_markdown,
    rewrite_context_note_with_ai,
    ENRICHMENT_UNAVAILABLE,
)
from .forms import StartAssessmentForm # Added import
from .services import (_expand_gtm_jargon, _build_question_guidance, _kickoff_playbook_generation, _log_access_denied, safe_get_session_or_403, _format_band_actions_markdown, _paginated_questions, _category_step_map, _first_incomplete_step, _compute_scores, _band_for_score, _is_session_complete, _save_snapshot)

LEGEND = {
    1: "No / Not in place",
    2: "Ad-hoc / Rarely",
    3: "In progress / Sometimes",
    4: "Consistent / Often",
    5: "Best-in-class / Always",
}

# ---------- helpers ----------

def _is_htmx(request):
    """Check if request is from HTMX."""
    return request.headers.get('HX-Request') == 'true'

def _get_template(request, base_template, partial_template=None):
    """Return partial template for HTMX requests, full template otherwise."""
    if _is_htmx(request) and partial_template:
        return partial_template
    return base_template







def require_session_ownership(view_func):
    """
    Decorator to enforce authenticated session access.
    Uses centralized workspace-aware session authorization.
    """
    @wraps(view_func)
    def wrapper(request, session_id, *args, **kwargs):
        if not request.user.is_authenticated:
            messages.error(request, "Please sign in to continue.")
            return redirect("account_login")

        session, is_authorized = safe_get_session_or_403(request, session_id)
        if not is_authorized:
            messages.error(request, "Access denied.")
            return redirect("gtm:history")
        
        # Pass both session object and session_id to preserve wrapped view signatures.
        return view_func(request, session, session_id, *args, **kwargs)
    return wrapper

def require_action_ownership(view_func):
    """
    Decorator to enforce ActionItem ownership.
    Checks if the user owns the session associated with the action item.
    """
    @wraps(view_func)
    def wrapper(request, action_id, *args, **kwargs):
        action = get_object_or_404(ActionItem, pk=action_id)

        if not request.user.is_authenticated:
            messages.error(request, "Please sign in to continue.")
            return redirect("account_login")

        if not action.session:
            messages.error(request, "Invalid action item.")
            return redirect("gtm:history")

        _, is_authorized = safe_get_session_or_403(request, action.session_id)
        if not is_authorized:
            messages.error(request, "Access denied.")
            return redirect("gtm:history")

        # Pass both action and action_id to view for correct signature
        return view_func(request, action, action_id, *args, **kwargs)
    return wrapper






    




def _remember_session(request, sess_uuid):
    request.session.setdefault("gtm_sessions", [])
    if str(sess_uuid) not in request.session["gtm_sessions"]:
        request.session["gtm_sessions"].append(str(sess_uuid))
        request.session.modified = True       


        
# ---------- Views ----------

def landing(request):
    # If user is authenticated and has workspace memberships, redirect to dashboard
    if request.user.is_authenticated:
        from .models_workspace import WorkspaceMembership
        user_memberships = WorkspaceMembership.objects.filter(user=request.user, is_active=True)
        if user_memberships.exists():
            # User has workspace access, redirect to dashboard
            first_workspace = user_memberships.first().workspace
            return redirect(f'/dashboard/?workspace={first_workspace.id}')
    
    return render(request, "gtm/landing.html")

@login_required
def start_assessment(request):
    # 0) Redirect to the most recent incomplete assessment instead of creating duplicates
    existing_incomplete = AssessmentSession.objects.filter(
        user=request.user,
        is_completed=False,
    ).order_by("-created_at").first()
    if existing_incomplete:
        messages.info(request, "You have an unfinished assessment. Resuming it now.")
        return redirect("gtm:resume", session_id=existing_incomplete.uuid)

    # 1) Daily cap (per authenticated user)
    today = timezone.now().date()
    max_assessments_per_day = getattr(settings, "MAX_ASSESSMENTS_PER_DAY", 3)
    daily_count = AssessmentSession.objects.filter(user=request.user, created_at__date=today).count()
    if daily_count >= max_assessments_per_day:
        messages.error(
            request,
            "Daily limit reached. Please try again tomorrow or contact us for extended access."
        )
        return redirect("gtm:history")

    # 2) Cooldown (time between new assessments per authenticated user)
    min_seconds_between_assessments = getattr(settings, "MIN_SECONDS_BETWEEN_ASSESSMENTS", 5 * 60)
    last_session = AssessmentSession.objects.filter(user=request.user).order_by("-created_at").first()
    if last_session:
        seconds_since_last = (timezone.now() - last_session.created_at).total_seconds()
        in_cooldown = seconds_since_last < min_seconds_between_assessments
        minutes_left = max(1, int((min_seconds_between_assessments - seconds_since_last) // 60)) if in_cooldown else 0
    else:
        in_cooldown = False
        minutes_left = 0

    if in_cooldown:
        messages.warning(
            request,
            f"Please wait about {minutes_left} minute(s) before starting another assessment."
        )
        return redirect("gtm:history")

    if request.method == "POST":
        form = StartAssessmentForm(request.POST)
        if form.is_valid():
            session = form.save(commit=False)
            session.user = request.user if request.user.is_authenticated else None
            session.owner_client_id = _client_id(request)
            
            # Associate with current workspace if available
            if hasattr(request, 'workspace') and request.workspace:
                session.workspace = request.workspace
            
            # Handle referrer separately as it comes from request.META, not directly from form POST data
            session.referrer = request.META.get("HTTP_REFERER", "")
            session.save()
            
            return redirect("gtm:resume", session_id=session.uuid)
        else:
            # If form is invalid, re-render the page with errors
            return render(request, "gtm/start.html", {
                "form": form,
                "is_htmx": _is_htmx(request)
            })

    else: # GET request
        form = StartAssessmentForm(initial={
            "utm_source": request.GET.get("utm_source", ""),
            "utm_medium": request.GET.get("utm_medium", ""),
            "utm_campaign": request.GET.get("utm_campaign", ""),
            # Referrer is set in save, but can be pre-filled from GET if desired
            "referrer": request.GET.get("referrer", ""),
        })
    return render(request, "gtm/start.html", {"form": form, "is_htmx": _is_htmx(request)})


@login_required
def edit_assessment_intro(request, session_id):
    """Lets the user revisit and update company/contact details (the 'Start Assessment'
    info) from within an in-progress assessment, then return to step 1."""
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        messages.error(request, "Access denied.")
        return redirect("gtm:history")

    if request.method == "POST":
        original_referrer = session.referrer
        form = StartAssessmentForm(request.POST, instance=session)
        if form.is_valid():
            updated_session = form.save(commit=False)
            updated_session.referrer = original_referrer
            updated_session.save()
            return redirect("gtm:assessment_step", session_id=session.uuid, step=1)
    else:
        form = StartAssessmentForm(instance=session)

    return render(request, "gtm/start.html", {
        "form": form,
        "edit_session": session,
        "is_htmx": _is_htmx(request),
    })


@login_required
def resume_assessment(request, session_id):
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        messages.error(request, "Access denied.")
        return redirect("gtm:history")

    step = _first_incomplete_step(session)
    session.current_step = step
    session.save(update_fields=["current_step"])
    return redirect("gtm:assessment_step", session_id=session.uuid, step=step)

@login_required
def resume_latest(request):
    s = AssessmentSession.objects.filter(user=request.user, is_completed=False).order_by("-created_at").first()
    if not s:
        return redirect("gtm:start")
    step = _first_incomplete_step(s)
    return redirect("gtm:assessment_step", session_id=s.uuid, step=step)

@login_required
def assessment_step(request, session_id, step: int):
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        messages.error(request, "Access denied.")
        return redirect("gtm:history")

    steps = _paginated_questions()
    total_steps = len(steps)
    if total_steps == 0:
        return redirect("gtm:results", session_id=session.uuid)

    # Do not allow skipping past the first incomplete step
    required_step = _first_incomplete_step(session)
    step = max(1, min(step, total_steps))
    if step > required_step:
        return redirect("gtm:assessment_step", session_id=session.uuid, step=required_step)

    questions = steps[step - 1]
    question_guidance = {q.id_code: _build_question_guidance(q) for q in questions}

    # Build a dynamic form with one integer field per question (1..5)
    context_fields = {}
    from django import forms
    class StepForm(Form):
        pass
    for q in questions:
        StepForm.base_fields[q.id_code] = IntegerField(
            min_value=1, max_value=5,
            widget=NumberInput(attrs={"class": "w-full border rounded px-3 py-2", "step": 1}),
            required=True,
            label=q.text
        )
        StepForm.base_fields[f"{q.id_code}_context_note"] = forms.CharField(
            widget=forms.Textarea(attrs={"class": "w-full border rounded px-3 py-2 text-sm", "rows": 3, "placeholder": "Please provide more info on this"}),
            required=False,
            label="Please provide more info on this"
        )

    # Pre-fill if answers exist
    initial = {}
    existing = {r.question_id: r for r in Response.objects.filter(session=session, question__in=questions)}
    for q in questions:
        if q.id in existing:
            initial[q.id_code] = existing[q.id].score
            initial[f"{q.id_code}_context_note"] = existing[q.id].context_note

    if request.method == "POST":
        form = StepForm(request.POST, initial=initial)
        if form.is_valid():
            low_score_responses = []
            # save/update answers for this step
            for q in questions:
                score = form.cleaned_data[q.id_code]
                context_note = form.cleaned_data.get(f"{q.id_code}_context_note", "")
                response_instance, created = Response.objects.update_or_create(
                    session=session, question=q, defaults={"score": score, "context_note": context_note}
                )
                if response_instance.score <= 2:
                    low_score_responses.append(response_instance)

            # Transition to next step
            next_step = step + 1
            session.current_step = min(next_step, total_steps)
            session.is_completed = _is_session_complete(session)
            
            # Ensure workspace association is set
            if hasattr(request, 'workspace') and request.workspace and not session.workspace:
                session.workspace = request.workspace
            
            session.save(update_fields=["current_step", "is_completed", "workspace"])

            if next_step > total_steps:
                return redirect("gtm:results", session_id=session.uuid)
            
            # For non-final steps, we still compute score for the progress bar or snapshot
            cat_scores, overall = _compute_scores(session)
            band = _band_for_score(overall)
            labels = [c["category"].name for c in cat_scores]
            values = [round(c["avg"], 2) for c in cat_scores]
            _save_snapshot(session, cat_scores, overall, band, labels, values)

            return redirect("gtm:assessment_step", session_id=session.uuid, step=next_step)
    else:
        form = StepForm(initial=initial)

    progress_pct = int((step - 1) / total_steps * 100)
    legend_html = "<br>".join([f"<b>{k}</b>: {v}" for k, v in LEGEND.items()])

    # Build per-step AI document analysis URLs
    from django.urls import reverse as _reverse
    is_delivery_step = (step == total_steps)
    category_slug = (questions[0].category.name.lower() if questions else '')

    if is_delivery_step:
        ai_upload_url  = _reverse('gtm:upload_delivery_document',  kwargs={'session_id': session.uuid})
        ai_analyze_url = _reverse('gtm:analyze_delivery_documents', kwargs={'session_id': session.uuid})
        ai_docs_url    = _reverse('gtm:get_delivery_documents',     kwargs={'session_id': session.uuid})
        ai_delete_tpl  = _reverse('gtm:delete_delivery_document',  kwargs={'session_id': session.uuid, 'doc_id': '00000000-0000-0000-0000-000000000000'})
    else:
        ai_upload_url  = _reverse('gtm:upload_category_document',   kwargs={'session_id': session.uuid, 'category': category_slug})
        ai_analyze_url = _reverse('gtm:analyze_category_documents', kwargs={'session_id': session.uuid, 'category': category_slug})
        ai_docs_url    = _reverse('gtm:get_category_documents',     kwargs={'session_id': session.uuid, 'category': category_slug})
        ai_delete_tpl  = _reverse('gtm:delete_category_document',  kwargs={'session_id': session.uuid, 'category': category_slug, 'doc_id': '00000000-0000-0000-0000-000000000000'})

    category_label = questions[0].category.name if questions else ''
    ai_panel_description = {
        'demand': 'Upload marketing plans, ICP docs, channel reports, attribution data, and CRM exports. AI will propose scores based on what it finds.',
        'conversion': 'Upload CRM pipeline reports, sales playbooks, win/loss data, and conversion analytics. AI will propose scores based on what it finds.',
        'delivery': 'Upload documents and let AI propose scores based on real evidence. Review and override before finishing.',
    }.get(category_slug, 'Upload documents and let AI propose scores based on real evidence. Review and override before finishing.')

    # Load any previously stored analysis so the AI panels survive navigation.
    prefilled_analysis = {}
    if is_delivery_step:
        completed = DeliveryDocument.objects.filter(
            session=session, analysis_status='complete'
        ).exclude(analysis_result={}).first()
        if completed:
            prefilled_analysis = completed.analysis_result
    elif category_slug:
        completed = CategoryDocument.objects.filter(
            session=session, category=category_slug, analysis_status='complete'
        ).exclude(analysis_result={}).first()
        if completed:
            prefilled_analysis = completed.analysis_result

    return render(request, "gtm/assessment_step.html", {
        "session": session, "form": form, "step": step, "total_steps": total_steps,
        "progress_pct": progress_pct, "legend": mark_safe(legend_html),
        "category": questions[0].category if questions else None,
        "is_htmx": _is_htmx(request),
        "context_fields": context_fields,
        "question_guidance": question_guidance,
        "is_delivery_step": is_delivery_step,
        "is_ai_step": True,
        "ai_upload_url": ai_upload_url,
        "ai_analyze_url": ai_analyze_url,
        "ai_docs_url": ai_docs_url,
        "ai_delete_tpl": ai_delete_tpl,
        "ai_category_label": category_label,
        "ai_panel_description": ai_panel_description,
        "prefilled_analysis": prefilled_analysis,
    })


@login_required
@require_POST
def rewrite_context_note(request, session_id):
    """Rewrite/summarize a context note and return preview text without persisting it."""
    import json

    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON payload."}, status=400)

    note_text = (payload.get("note_text") or "").strip()
    question_text = (payload.get("question_text") or "").strip()
    mode = (payload.get("mode") or "rewrite").strip().lower()

    if not note_text:
        return JsonResponse({"success": False, "error": "Please add some text first."}, status=400)

    rewritten = rewrite_context_note_with_ai(note_text=note_text, question_text=question_text, mode=mode)
    if not rewritten:
        return JsonResponse({"success": False, "error": "Could not rewrite note right now."}, status=500)

    return JsonResponse({
        "success": True,
        "rewritten_text": rewritten,
        "mode": mode,
    })

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

    step_map = {cat.id: idx + 1 for idx, cat in enumerate(Category.objects.all().order_by("id"))}
    strengths_categories = sorted(cat_scores, key=lambda x: x["avg"], reverse=True)[:3]
    focus_categories = sorted(cat_scores, key=lambda x: x["avg"])[:3]

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
                # 🆕 NEW: Include the AI insight from the Response object
                "ai_insight": r.ai_insight or "",
            })
    weakest_questions = sorted(all_rows, key=lambda x: x["weighted"])[:3]

    labels = [c["category"].name for c in cat_scores]
    values = [round(c["avg"], 2) for c in cat_scores]

    # ── Scoring engine: opportunity ranking + pattern detection ──────────────
    from .scoring_engine import build_recommendation_context
    _q_scores = {
        r.question.id_code: r.score
        for r in Response.objects.filter(session=session).select_related("question")
    }
    scoring_context = build_recommendation_context(_q_scores) if _q_scores else {}

    # -----------------------------
    # 🧩 Tool Recommendations Logic (Optimized to prevent N+1)
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
    
    # ⚡ FALLBACK: If insufficient recommendations, add generic tools from weakest categories
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
    # ✅ Render band actions (using centralized formatter)
    # -----------------------------
    band_actions_html = _format_band_actions_markdown(
        getattr(band, "actions_markdown", "") if band else ""
    )

    # -----------------------------
    # 📊 Save snapshot (single source of truth)
    # -----------------------------
    # Let _save_snapshot handle update_or_create and return the instance
    snap = _save_snapshot(session, cat_scores, overall, band, labels, values)

    # -----------------------------
    # 🤖 Generate AI insights for low-scoring questions (if not already generated)
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

    # 🤖 TRIGGER BATCH ASYNC GENERATION (one thread for all insights, not one per response)
    if responses_needing_insight:
        resp_ids_for_batch = [r.id for r in responses_needing_insight]

        def gen_batch_async(r_ids):
            from .models import Response as ResponseModel
            fresh_responses = list(
                ResponseModel.objects.filter(id__in=r_ids).select_related("question", "session__snapshot")
            )
            generate_diagnostic_insights_batch(fresh_responses)

        run_in_background(gen_batch_async, resp_ids_for_batch, name="batch_diagnostic_insights")

    
    # -----------------------------
    # 🤖 Trigger AI playbook generation in background (non-blocking)
    # -----------------------------
    playbook_raw = (snap.ai_playbook or "").strip() if snap else ""
    # The final fallback stored when Gemini fails — treat as if no playbook exists
    is_generic_fallback = playbook_raw.startswith("No AI-generated playbook available yet.")

    if snap and (not playbook_raw or is_generic_fallback):
        if is_generic_fallback:
            snap.ai_playbook = ""
            snap.ai_playbook_status = "pending"
            snap.save(update_fields=["ai_playbook", "ai_playbook_status"])
        _kickoff_playbook_generation(snap, session_id=session.uuid)

    # Render AI playbook HTML for the "Recommended Next Moves" card
    ai_playbook_html = ""
    if snap and playbook_raw and not is_generic_fallback:
        src = _normalize_ai_playbook_markdown(snap.ai_playbook)
        ai_playbook_html = mark_safe(md.markdown(src, extensions=["extra", "sane_lists"]))

    from django.urls import reverse as _reverse
    next_moves_poll_url = _reverse("gtm:next_moves_content", kwargs={"session_id": session.uuid})
    snap_status = getattr(snap, "ai_playbook_status", "pending") if snap else "pending"
    total_steps = len(_paginated_questions())

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
        "is_htmx": _is_htmx(request),
        "snap": snap,
        "next_moves_poll_url": next_moves_poll_url,
        "next_moves_failed": snap_status == "failed" or is_generic_fallback,
    })

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
            from .ai_services import generate_enrichment_sections

            def _run(snap_id):
                from .models import ResultSnapshot as RS
                generate_enrichment_sections(RS.objects.get(id=snap_id))

            run_in_background(_run, snap.id, name="enrichment_sections")

    fin_html = comp_html = ""
    fin_failed = (snap.ai_financial_summary == ENRICHMENT_UNAVAILABLE)
    comp_failed = (snap.ai_competitor_analysis == ENRICHMENT_UNAVAILABLE)

    if snap.ai_financial_summary and not fin_failed:
        fin_html = mark_safe(md.markdown(_normalize_ai_playbook_markdown(snap.ai_financial_summary), extensions=["extra", "sane_lists"]))
    if snap.ai_competitor_analysis and not comp_failed:
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
    # 🧩 Format Recommended Next Moves (using centralized formatter)
    # -----------------------------
    band_actions_html = _format_band_actions_markdown(
        getattr(band, "actions_markdown", "") if band else ""
    )

    # Scoring engine context for company-specific next moves bullets
    from .scoring_engine import build_recommendation_context
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
        from .ai_services import extract_tasks_from_playbook
        _snap_for_tasks = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()
        _playbook_text = (_snap_for_tasks.ai_playbook or "").strip() if _snap_for_tasks else ""
        _company = session.company_name or "your company"
        _patterns = scoring_context.get("patterns", [])
        _severity_days = {"Critical": 14, "High": 28, "Medium": 42}
        _tasks_to_create = []

        # Try 90%: extract ~4 tasks from AI playbook
        _ai_tasks = extract_tasks_from_playbook(_playbook_text, _company, n=4) if _playbook_text else []

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
    # 🤖 AI Playbook Rendering (+ optional lazy-generate)
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
            # Only spawn a thread if generation hasn't started yet
            def gen_diagnostic_async(resp_id):
                from .models import Response
                from .ai_services import generate_diagnostic_insight
                r = Response.objects.select_related("question", "session__snapshot").get(id=resp_id)
                generate_diagnostic_insight(r)

            run_in_background(gen_diagnostic_async, response.id, name="diagnostic_insight_poll")
        # If status is "generating" or "failed", don't spawn another thread

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

@login_required
def history(request):
    qs = AssessmentSession.objects.filter(user=request.user).order_by("-created_at")
        
    # Use select_related to minimize queries
    qs = qs.select_related('snapshot__band')
    
    rows = []
    sessions_needing_score_computation = []

    for s in qs:
        snap = getattr(s, 'snapshot', None)
        if snap:
            overall = snap.overall
            band = snap.band
        elif s.is_completed:
            sessions_needing_score_computation.append(s)
            overall = None # Will be computed later
            band = None # Will be computed later
        else:
            overall = None
            band = None
            
        rows.append({"session": s, "overall": (round(overall,1) if overall is not None else None), "band": band})

    # Batch prefetch responses for sessions needing score computation
    if sessions_needing_score_computation:
        session_ids_needing_comp = [s.uuid for s in sessions_needing_score_computation]
        # Prefetch all responses and their related questions and categories
        responses_qs = Response.objects.filter(
            session_id__in=session_ids_needing_comp
        ).select_related('question__category')
        
        # Organize responses by session for efficient lookup
        responses_by_session = {}
        for r in responses_qs:
            responses_by_session.setdefault(r.session_id, []).append(r)
        
        # Re-iterate through rows to fill in computed scores and bands
        for row in rows:
            s = row['session']
            if s.is_completed and not getattr(s, 'snapshot', None):
                # Now _compute_scores can use the prefetched responses
                # Note: _compute_scores needs to be adapted to accept preloaded responses
                # or ensure its internal queries don't hit DB if data is already there.
                # For now, we assume _compute_scores is efficient enough per session given prefetched data.
                # A more thorough change would modify _compute_scores to take `responses_qs` directly.
                cat_scores, overall = _compute_scores(s) # This will still query categories, but responses are prefetched
                band = _band_for_score(overall)
                row['overall'] = round(overall, 1) if overall is not None else None
                row['band'] = band
        
    return render(request, "gtm/history.html", {"rows": rows, "is_htmx": _is_htmx(request)})


@login_required
@require_POST
def upload_strategic_evidence(request, session_id):
    """
    Handles file uploads (Images/PDFs) for strategic GTM audits.
    """
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)
        
    if 'file' not in request.FILES:
        return JsonResponse({"success": False, "error": "No file uploaded."}, status=400)
        
    uploaded_file = request.FILES['file']
    file_type = request.POST.get('file_type', 'other')
    
    # Basic size limit (5MB)
    if uploaded_file.size > 5 * 1024 * 1024:
        return JsonResponse({"success": False, "error": "File too large (max 5MB)."}, status=400)
    
    try:
        from .models import GTMFile
        gtm_file = GTMFile.objects.create(
            session=session,
            file=uploaded_file,
            file_type=file_type
        )
        
        # 🤖 TRIGGER ASYNC INITIAL AUDIT
        # This caches the result so the chat agent doesn't have to wait for a 10s API call
        # Build context for the audit
        cat_scores, overall = _compute_scores(session)
        band = _band_for_score(overall)
        context_str = f"Company GTM Score: {overall}/100. Stage: {band.stage if band else 'N/A'}"

        def run_initial_audit(file_id, ctx):
            from .models import GTMFile
            from .ai_auditor import perform_gtm_visual_audit
            f = GTMFile.objects.get(id=file_id)
            perform_gtm_visual_audit(f, session_context=ctx)

        run_in_background(run_initial_audit, gtm_file.id, context_str, name="initial_visual_audit")
        
        return JsonResponse({
            "success": True,
            "message": f"Successfully uploaded {gtm_file.get_file_type_display()}. Audit in progress...",
            "file_id": str(gtm_file.id)
        })
        
    except Exception as e:
        log_error("Evidence Upload Failure", e)
        return JsonResponse({"success": False, "error": "Could not upload file."}, status=500)

@login_required
@require_POST
def upload_delivery_document(request, session_id):
    """Upload a document for the Delivery step AI analysis. Text extraction runs async."""
    import os as _os

    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    if 'file' not in request.FILES:
        return JsonResponse({"success": False, "error": "No file provided."}, status=400)

    uploaded = request.FILES['file']

    if uploaded.size > 10 * 1024 * 1024:
        return JsonResponse({"success": False, "error": "File too large (max 10 MB)."}, status=400)

    ext_map = {
        '.csv': 'csv', '.xlsx': 'xlsx', '.xls': 'xlsx',
        '.pdf': 'pdf', '.docx': 'docx', '.doc': 'docx',
        '.txt': 'txt', '.md': 'txt', '.log': 'txt',
        '.json': 'json',
        '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image',
    }
    ext = _os.path.splitext(uploaded.name.lower())[1]
    file_type = ext_map.get(ext, 'other')

    doc = DeliveryDocument.objects.create(
        session=session,
        file=uploaded,
        original_filename=uploaded.name,
        file_size=uploaded.size,
        file_type=file_type,
        analysis_status='uploaded',
    )

    # New file means any prior analysis is stale — clear it so the server
    # doesn't serve old results on the next page load.
    DeliveryDocument.objects.filter(session=session, analysis_status='complete').exclude(id=doc.id).update(
        analysis_result={}
    )

    def _extract_in_background(doc_id, ftype):
        from .delivery_analyzer import extract_text_from_path
        from .ai_services import _get_client
        d = DeliveryDocument.objects.get(id=doc_id)
        client = _get_client() if ftype == 'image' else None
        text = extract_text_from_path(d.file.path, ftype, client, "gemini-2.5-flash")
        d.extracted_text = text
        d.save(update_fields=['extracted_text'])

    run_in_background(_extract_in_background, str(doc.id), file_type, name="delivery_doc_extract")

    return JsonResponse({
        "success": True,
        "doc_id": str(doc.id),
        "filename": doc.original_filename,
        "file_type": file_type,
        "file_size": doc.file_size,
    })


@login_required
@require_POST
def analyze_delivery_documents(request, session_id):
    """Run Gemini analysis on all uploaded delivery documents and return per-question scores."""
    from .delivery_analyzer import analyze_delivery_documents as _run_analysis

    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    docs = DeliveryDocument.objects.filter(session=session)
    if not docs.exists():
        return JsonResponse({"success": False, "error": "No documents uploaded yet."}, status=400)

    docs_with_text = docs.filter(extracted_text__gt='')
    if not docs_with_text.exists():
        return JsonResponse(
            {"success": False, "error": "Documents are still being processed. Please wait a moment and try again."},
            status=400,
        )

    result = _run_analysis(session)
    if not result or "_error" in result:
        error_detail = result.get("_error", "") if result else ""
        return JsonResponse(
            {"success": False, "error": f"Analysis failed: {error_detail}" if error_detail else "Could not analyse documents. Please try again."},
            status=500,
        )

    docs_with_text.update(analysis_status='complete', analysis_result=result)

    return JsonResponse({"success": True, "scores": result})


@login_required
def get_delivery_documents(request, session_id):
    """Return the list of uploaded delivery documents for this session (GET)."""
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    docs = list(
        DeliveryDocument.objects.filter(session=session).values(
            'id', 'original_filename', 'file_size', 'file_type', 'analysis_status',
            'analysis_result',
        )
    )
    for d in docs:
        d['id'] = str(d['id'])
        if d['analysis_status'] != 'complete':
            d.pop('analysis_result', None)

    return JsonResponse({"success": True, "documents": docs})


@login_required
@require_POST
def delete_delivery_document(request, session_id, doc_id):
    """Delete a previously uploaded delivery document."""
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    doc = get_object_or_404(DeliveryDocument, id=doc_id, session=session)
    try:
        doc.file.delete(save=False)
    except Exception:
        pass
    doc.delete()
    # File removed means prior analysis is stale — clear it from remaining docs.
    DeliveryDocument.objects.filter(session=session, analysis_status='complete').update(
        analysis_result={}
    )
    return JsonResponse({"success": True})


# ---------------------------------------------------------------------------
# Generic Category Document Analysis (Demand / Conversion)
# ---------------------------------------------------------------------------

_VALID_CATEGORIES = {'demand', 'conversion'}

_CATEGORY_EXT_MAP = {
    '.csv': 'csv', '.xlsx': 'xlsx', '.xls': 'xlsx',
    '.pdf': 'pdf', '.docx': 'docx', '.doc': 'docx',
    '.txt': 'txt', '.md': 'txt', '.log': 'txt',
    '.json': 'json',
    '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image',
}


@login_required
@require_POST
def upload_category_document(request, session_id, category):
    """Upload a document for Demand or Conversion AI analysis. Text extraction runs async."""
    import os as _os

    if category not in _VALID_CATEGORIES:
        return JsonResponse({"success": False, "error": "Invalid category."}, status=400)

    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    if 'file' not in request.FILES:
        return JsonResponse({"success": False, "error": "No file provided."}, status=400)

    uploaded = request.FILES['file']
    if uploaded.size > 10 * 1024 * 1024:
        return JsonResponse({"success": False, "error": "File too large (max 10 MB)."}, status=400)

    ext = _os.path.splitext(uploaded.name.lower())[1]
    file_type = _CATEGORY_EXT_MAP.get(ext, 'other')

    doc = CategoryDocument.objects.create(
        session=session,
        category=category,
        file=uploaded,
        original_filename=uploaded.name,
        file_size=uploaded.size,
        file_type=file_type,
        analysis_status='uploaded',
    )

    # New file means any prior analysis is stale — clear it so the server
    # doesn't serve old results on the next page load.
    CategoryDocument.objects.filter(
        session=session, category=category, analysis_status='complete'
    ).exclude(id=doc.id).update(analysis_result={})

    def _extract_in_background(doc_id, ftype):
        from .delivery_analyzer import extract_text_from_path
        from .ai_services import _get_client
        d = CategoryDocument.objects.get(id=doc_id)
        client = _get_client() if ftype == 'image' else None
        text = extract_text_from_path(d.file.path, ftype, client, "gemini-2.5-flash")
        d.extracted_text = text
        d.save(update_fields=['extracted_text'])

    run_in_background(_extract_in_background, str(doc.id), file_type, name="category_doc_extract")

    return JsonResponse({
        "success": True,
        "doc_id": str(doc.id),
        "filename": doc.original_filename,
        "file_type": file_type,
        "file_size": doc.file_size,
    })


@login_required
@require_POST
def analyze_category_documents(request, session_id, category):
    """Run Gemini analysis on uploaded documents for a Demand or Conversion step."""
    if category not in _VALID_CATEGORIES:
        return JsonResponse({"success": False, "error": "Invalid category."}, status=400)

    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    docs = CategoryDocument.objects.filter(session=session, category=category)
    if not docs.exists():
        return JsonResponse({"success": False, "error": "No documents uploaded yet."}, status=400)

    docs_with_text = docs.filter(extracted_text__gt='')
    if not docs_with_text.exists():
        return JsonResponse(
            {"success": False, "error": "Documents are still being processed. Please wait a moment and try again."},
            status=400,
        )

    from .category_analyzer import analyze_category_documents as _run_analysis
    result = _run_analysis(session, category)
    if not result or "_error" in result:
        error_detail = result.get("_error", "") if result else ""
        return JsonResponse(
            {"success": False, "error": f"Analysis failed: {error_detail}" if error_detail else "Could not analyse documents. Please try again."},
            status=500,
        )

    docs_with_text.update(analysis_status='complete', analysis_result=result)
    return JsonResponse({"success": True, "scores": result})


@login_required
def get_category_documents(request, session_id, category):
    """Return uploaded category documents for this session (GET)."""
    if category not in _VALID_CATEGORIES:
        return JsonResponse({"success": False, "error": "Invalid category."}, status=400)

    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    docs = list(
        CategoryDocument.objects.filter(session=session, category=category).values(
            'id', 'original_filename', 'file_size', 'file_type', 'analysis_status',
            'analysis_result',
        )
    )
    for d in docs:
        d['id'] = str(d['id'])
        if d['analysis_status'] != 'complete':
            d.pop('analysis_result', None)

    return JsonResponse({"success": True, "documents": docs})


@login_required
@require_POST
def delete_category_document(request, session_id, category, doc_id):
    """Delete a previously uploaded category document."""
    if category not in _VALID_CATEGORIES:
        return JsonResponse({"success": False, "error": "Invalid category."}, status=400)

    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        return JsonResponse({"success": False, "error": "Access denied."}, status=403)

    doc = get_object_or_404(CategoryDocument, id=doc_id, session=session, category=category)
    try:
        doc.file.delete(save=False)
    except Exception:
        pass
    doc.delete()
    # File removed means prior analysis is stale — clear it from remaining docs.
    CategoryDocument.objects.filter(
        session=session, category=category, analysis_status='complete'
    ).update(analysis_result={})
    return JsonResponse({"success": True})


@require_POST
@require_session_ownership
def cancel_assessment(request, session, session_id):
    """Cancel an in-progress assessment and remove it from history."""
    if session.is_completed:
        messages.warning(request, "Completed assessments cannot be canceled.")
        return redirect("gtm:history")

    company_label = session.company_name or "this assessment"
    session.delete()
    messages.success(request, f"Canceled {company_label}.")
    return redirect("gtm:history")


@require_POST
@require_session_ownership
def action_add(request, session, session_id):
    """Add a new action item to the session."""
    note = request.POST.get("note","").strip()
    
    question_id = request.POST.get("question_id")
    if note:
        ActionItem.objects.create(
            session=session,
            question=Question.objects.filter(id=question_id).first() if question_id else None,
            note=note,
            created_by=request.user
        )
    return redirect("gtm:playbook", session_id=session.uuid)

@require_POST
@require_action_ownership
def action_toggle(request, action, action_id):
    """Toggle action item status between todo and done."""
    action.status = "done" if action.status != "done" else "todo"
    action.save(update_fields=["status"])
    return redirect("gtm:playbook", session_id=action.session.uuid)

@require_POST
@require_action_ownership
def action_update(request, action, action_id):
    """Update action item fields (note, owner, status, due_date)."""
    note = request.POST.get("note", "").strip()
    owner = request.POST.get("owner", "").strip()
    status = request.POST.get("status", action.status)
    due_raw = request.POST.get("due_date", "").strip()

    action.note = note or action.note
    action.owner = owner
    if status in dict(ActionItem.STATUS_CHOICES):
        action.status = status
    # parse date safely (YYYY-MM-DD from <input type="date">)
    if due_raw:
        try:
            action.due_date = datetime.strptime(due_raw, "%Y-%m-%d").date()
        except ValueError:
            pass  # ignore bad date input
    else:
        action.due_date = None

    action.save()
    return redirect("gtm:playbook", session_id=action.session.uuid)

@require_POST
@require_action_ownership
def action_delete(request, action, action_id):
    """Delete action item."""
    sess_id = action.session.uuid
    action.delete()
    return redirect("gtm:playbook", session_id=sess_id)

# ================================================================
# AI CHAT ASSISTANT
# ================================================================
from django.views.decorators.http import require_http_methods
from .models import ChatMessage


# ================================================================
# USER PROFILE & AUTHENTICATION
# ================================================================

@login_required
def profile(request):
    """Display user profile with stats and recent activity"""
    user = request.user
    
    # Get user's assessments
    assessments = AssessmentSession.objects.filter(user=user).select_related('snapshot')
    
    # Calculate stats
    total_assessments = assessments.count()
    completed_assessments = assessments.filter(is_completed=True).count()
    in_progress_assessments = assessments.filter(is_completed=False).count()
    
    # Calculate average score from completed assessments with snapshots
    completed_with_scores = assessments.filter(
        is_completed=True,
        snapshot__isnull=False
    )
    avg_score = completed_with_scores.aggregate(
        avg=Avg('snapshot__overall')
    )['avg']
    
    # Get recent assessments (last 5)
    recent_assessments = assessments.order_by('-created_at')[:5]
    
    return render(request, "gtm/profile.html", {
        "total_assessments": total_assessments,
        "completed_assessments": completed_assessments,
        "in_progress_assessments": in_progress_assessments,
        "avg_score": avg_score,
        "recent_assessments": recent_assessments,
        "is_htmx": _is_htmx(request),
    })

def logout_view(request):
    """Logout user and redirect to landing page"""
    logout(request)
    messages.success(request, "You have been successfully logged out.")
    return redirect("gtm:landing")