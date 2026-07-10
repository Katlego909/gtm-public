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

def landing(request):
    # If user is authenticated and has workspace memberships, redirect to dashboard
    if request.user.is_authenticated:
        from ..models_workspace import WorkspaceMembership
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

