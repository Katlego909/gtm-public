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
from .models import AssessmentSession, Question, Response, Category, RecommendationBand, ActionItem, ToolRecommendation, ResultSnapshot
from django.utils.safestring import mark_safe
import markdown as md
import math
import re
from django.http import HttpResponse
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
from functools import wraps

from django.shortcuts import get_object_or_404
from .utils_pdf import render_gtm_report_pdf_response

from .ai_services import generate_playbook_with_gemini, generate_diagnostic_insight

LEGEND = {
    1: "No / Not in place",
    2: "Ad-hoc / Rarely",
    3: "In progress / Sometimes",
    4: "Consistent / Often",
    5: "Best-in-class / Always",
}

# ---------- helpers ----------

def _client_id(request):
    """Extract client ID from cookie for anonymous user tracking."""
    return request.COOKIES.get("gtm_client", "")

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
    Decorator to enforce session ownership.
    Checks if authenticated user owns the session or if anonymous client ID matches.
    """
    @wraps(view_func)
    def wrapper(request, session_id, *args, **kwargs):
        session = get_object_or_404(AssessmentSession, pk=session_id)
        
        # Check ownership
        if request.user.is_authenticated:
            if session.user != request.user:
                messages.error(request, "Access denied.")
                return redirect("gtm:history")
        elif session.owner_client_id != _client_id(request):
            messages.error(request, "Access denied.")
            return redirect("gtm:history")
        
        # Pass session to view to avoid re-querying
        return view_func(request, session, *args, **kwargs)
    return wrapper

def require_action_ownership(view_func):
    """
    Decorator to enforce ActionItem ownership.
    Checks if the user owns the session associated with the action item.
    """
    @wraps(view_func)
    def wrapper(request, action_id, *args, **kwargs):
        action = get_object_or_404(ActionItem, pk=action_id)
        
        # Check ownership via session
        if request.user.is_authenticated:
            if action.session.user != request.user:
                messages.error(request, "Access denied.")
                return redirect("gtm:history")
        elif action.session.owner_client_id != _client_id(request):
            messages.error(request, "Access denied.")
            return redirect("gtm:history")
        
        # Pass action to view to avoid re-querying
        return view_func(request, action, *args, **kwargs)
    return wrapper

def _format_band_actions_markdown(markdown_text):
    """
    Centralized markdown formatting for band actions.
    Applies consistent formatting rules across all views.
    """
    if not markdown_text:
        return ""
    
    # Replace heading keywords with bold markdown
    actions_md = markdown_text.replace("Action Plan:", "**Action Plan**")
    actions_md = actions_md.replace("Recommended Tools:", "**Recommended Tools**")
    
    # Force blank line before bullets for proper rendering
    actions_md = re.sub(r"\n-\s*", "\n\n• ", actions_md)
    
    # Clean up excessive newlines
    actions_md = re.sub(r"\n{3,}", "\n\n", actions_md).strip()
    
    # Render markdown to HTML
    try:
        return mark_safe(md.markdown(actions_md, extensions=["extra", "sane_lists"]))
    except Exception:
        # Fallback to simple line break conversion
        return mark_safe(actions_md.replace("\n", "<br>"))

def _paginated_questions():
    """Return a list of steps, each = list[Question]. One category per step."""
    return [list(cat.questions.all().order_by("id"))
            for cat in Category.objects.all().order_by("id")]
    
def _category_step_map():
    """Map category id → step number (1-based) for deep-linking to the wizard."""
    return {cat.id: idx + 1 for idx, cat in enumerate(Category.objects.all().order_by("id"))}    

def _first_incomplete_step(session):
    steps = _paginated_questions()
    for idx, qs in enumerate(steps, start=1):
        answered = Response.objects.filter(session=session, question__in=qs).count()
        if answered < len(qs):
            return idx
    return max(1, len(steps))  # all answered → last step

def _compute_scores(session: AssessmentSession):
    # 1. Fetch all category weights and map to ID for overall calculation
    all_cats = Category.objects.all().order_by("id")
    total_w = sum(c.weight for c in all_cats) or 1.0
    cat_weight_map = {c.id: c.weight for c in all_cats}
    cat_name_map = {c.id: c.name for c in all_cats}

    # 2. Use a single efficient query to get category-level weighted scores
    #    (Groups responses by category and calculates the weighted average per group)
    category_results = (
        Response.objects
        .filter(session=session)
        .values('question__category_id')
        .annotate(
            total_weighted_score=Sum(F('score') * F('question__weight')),
            total_weight=Sum('question__weight')
        )
        .order_by('question__category_id')
    )

    cat_scores = []
    overall = 0.0
    
    # Pre-populate with all categories (in case some have no responses)
    scores_by_id = {c.id: {"category": c, "avg": 0.0} for c in all_cats}

    for row in category_results:
        cat_id = row['question__category_id']
        num = row['total_weighted_score']
        den = row['total_weight']
        avg = num / den if den else 0.0
        
        # Update the structure with the calculated average
        scores_by_id[cat_id].update({"avg": avg})
        
        # Calculate overall contribution
        weight = cat_weight_map.get(cat_id, 0)
        overall += (avg / 5.0) * (weight / total_w) * 100.0
        
    # Convert the map back to a list of scores
    cat_scores = list(scores_by_id.values())

    return cat_scores, overall

def _band_for_score(score):
    return RecommendationBand.objects.filter(min_score__lte=score, max_score__gte=score).first()

def _remember_session(request, sess_uuid):
    request.session.setdefault("gtm_sessions", [])
    if str(sess_uuid) not in request.session["gtm_sessions"]:
        request.session["gtm_sessions"].append(str(sess_uuid))
        request.session.modified = True       

def _is_session_complete(session: AssessmentSession) -> bool:
    total_q = Question.objects.count()
    if total_q == 0:
        return False
    answered_q = (Response.objects
                  .filter(session=session)
                  .values("question_id").distinct().count())
    return answered_q == total_q

def _save_snapshot(session, cat_scores, overall, band, labels, values):
    with transaction.atomic():
        snap, _ = ResultSnapshot.objects.update_or_create(
            session=session,
            defaults={
                "overall": round(overall, 1),
                "band": band,
                "band_stage": (band.stage if band else ""),
                "band_headline": (band.headline if band else ""),
                "category_breakdown": [
                    {"category": c["category"].name, "avg": round(c["avg"], 2)}
                    for c in cat_scores
                ],
                "radar_labels": labels,
                "radar_values": values,

                # firmographics copied from the session
                "company_name": session.company_name or "",
                "industry": session.industry or "",
                "website": getattr(session, "website", "") or "",
                "contact_name": getattr(session, "contact_name", "") or "",
                "contact_email": getattr(session, "contact_email", "") or "",
                "contact_role": getattr(session, "contact_role", "") or "",
                "phone": getattr(session, "phone", "") or "",
                "company_size": getattr(session, "company_size", "") or "",
                "revenue_range": getattr(session, "revenue_range", "") or "",
                "country": getattr(session, "country", "") or "",
                "crm": getattr(session, "crm", "") or "",
                "utm_source": getattr(session, "utm_source", "") or "",
                "utm_medium": getattr(session, "utm_medium", "") or "",
                "utm_campaign": getattr(session, "utm_campaign", "") or "",
                "referrer": getattr(session, "referrer", "") or "",
            }
        )
    return snap
        
# ---------- Views ----------

def landing(request):
    return render(request, "gtm/landing.html")

@login_required
def start_assessment(request):
    # Identify the anonymous "user" via your gtm_client cookie
    cid = _client_id(request)  # already defined in your file

    # 0) Redirect to the most recent incomplete assessment instead of creating duplicates
    existing_incomplete = AssessmentSession.objects.filter(
        owner_client_id=cid, is_completed=False
    ).order_by("-created_at").first()
    if existing_incomplete:
        messages.info(request, "You have an unfinished assessment. Resuming it now.")
        return redirect("gtm:resume", session_id=existing_incomplete.uuid)

    # 1) Daily cap (per browser/client)
    MAX_ASSESSMENTS_PER_DAY = 3
    today = timezone.now().date()
    daily_count = AssessmentSession.objects.filter(
        owner_client_id=cid,
        created_at__date=today
    ).count()
    if daily_count >= MAX_ASSESSMENTS_PER_DAY:
        messages.error(
            request,
            "Daily limit reached. Please try again tomorrow or contact us for extended access."
        )
        return redirect("gtm:history")

    # 2) Cooldown (time between new assessments)
    MIN_SECONDS_BETWEEN_ASSESSMENTS = 5 * 60  # 5 minutes
    last_session = AssessmentSession.objects.filter(
        owner_client_id=cid
    ).order_by("-created_at").first()
    if last_session:
        seconds_since_last = (timezone.now() - last_session.created_at).total_seconds()
        if seconds_since_last < MIN_SECONDS_BETWEEN_ASSESSMENTS:
            wait_left = int(MIN_SECONDS_BETWEEN_ASSESSMENTS - seconds_since_last)
            minutes_left = max(1, wait_left // 60)
            messages.warning(
                request,
                f"Please wait about {minutes_left} minute(s) before starting another assessment."
            )
            return redirect("gtm:history")

    if request.method == "POST":
        company  = request.POST.get("company_name", "")
        industry = request.POST.get("industry", "")

        # 🔹 new fields (optional)
        website       = request.POST.get("website", "")
        contact_name  = request.POST.get("contact_name", "")
        contact_email = request.POST.get("contact_email", "")
        contact_role  = request.POST.get("contact_role", "")
        phone         = request.POST.get("phone", "")
        company_size  = request.POST.get("company_size", "")
        revenue_range = request.POST.get("revenue_range", "")
        country       = request.POST.get("country", "")
        crm           = request.POST.get("crm", "")
        notes         = request.POST.get("notes", "")

        # acquisition (helpful if you add hidden inputs from querystring)
        utm_source   = request.POST.get("utm_source", "")
        utm_medium   = request.POST.get("utm_medium", "")
        utm_campaign = request.POST.get("utm_campaign", "")
        referrer     = request.META.get("HTTP_REFERER", "")

        session = AssessmentSession.objects.create(
            company_name=company,
            industry=industry,
            website=website,
            contact_name=contact_name,
            contact_email=contact_email,
            contact_role=contact_role,
            phone=phone,
            company_size=company_size,
            revenue_range=revenue_range,
            country=country,
            crm=crm,
            notes=notes,
            utm_source=utm_source,
            utm_medium=utm_medium,
            utm_campaign=utm_campaign,
            referrer=referrer,
            user=request.user if request.user.is_authenticated else None,
            owner_client_id=_client_id(request),
        )
        
        return redirect("gtm:resume", session_id=session.uuid)

    return render(request, "gtm/start.html", {"is_htmx": _is_htmx(request)})


def resume_assessment(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    step = _first_incomplete_step(session)
    session.current_step = step
    session.save(update_fields=["current_step"])
    return redirect("gtm:assessment_step", session_id=session.uuid, step=step)

def resume_latest(request):
    cid = _client_id(request)
    s = AssessmentSession.objects.filter(owner_client_id=cid, is_completed=False).order_by("-created_at").first()
    if not s:
        return redirect("gtm:start")
    step = _first_incomplete_step(s)
    return redirect("gtm:assessment_step", session_id=s.uuid, step=step)

def assessment_step(request, session_id, step: int):
    session = get_object_or_404(AssessmentSession, pk=session_id)
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

    # Build a dynamic form with one integer field per question (1..5)
    class StepForm(Form):
        pass
    for q in questions:
        StepForm.base_fields[q.id_code] = IntegerField(
            min_value=1, max_value=5,
            widget=NumberInput(attrs={"class": "w-full border rounded px-3 py-2", "step": 1}),
            required=True,
            label=q.text
        )

    # Pre-fill if answers exist
    initial = {}
    existing = {r.question_id: r.score
                for r in Response.objects.filter(session=session, question__in=questions)}
    for q in questions:
        if q.id in existing:
            initial[q.id_code] = existing[q.id]

    if request.method == "POST":
        form = StepForm(request.POST, initial=initial)
        if form.is_valid():
            # Collect responses that need AI insights
            low_score_responses = []
            
            # save/update answers for this step
            for q in questions:
                score = form.cleaned_data[q.id_code]
                
                # Use update_or_create to get the Response instance
                response_instance, created = Response.objects.update_or_create(
                    session=session, question=q, defaults={"score": score}
                )
                
                # Track low-scoring responses for AI insight generation
                if response_instance.score <= 2:
                    low_score_responses.append(response_instance)
            
            # ----------------------------------------------------
            # 🔧 OPTIMIZATION: Compute scores and save snapshot ONCE after all responses
            # ----------------------------------------------------
            cat_scores, overall = _compute_scores(session)
            band = _band_for_score(overall)
            
            # Re-calculate radar data (as required by _save_snapshot)
            labels = [c["category"].name for c in cat_scores]
            values = [round(c["avg"], 2) for c in cat_scores]
            
            # Save the snapshot now so 'session.snapshot' is current
            _save_snapshot(session, cat_scores, overall, band, labels, values)
            # ----------------------------------------------------
            
            # update progress + completion flag (data-driven)
            next_step = step + 1
            session.current_step = min(next_step, total_steps)
            session.is_completed = _is_session_complete(session)
            # The firmographics were already saved in _save_snapshot, so we only need
            # to save the step/completion flags.
            session.save(update_fields=["current_step", "is_completed"])

            if next_step > total_steps:
                return redirect("gtm:results", session_id=session.uuid)
            
            return redirect("gtm:assessment_step", session_id=session.uuid, step=next_step)
    else:
        form = StepForm(initial=initial)

    progress_pct = int((step - 1) / total_steps * 100)
    legend_html = "<br>".join([f"<b>{k}</b>: {v}" for k, v in LEGEND.items()])

    return render(request, "gtm/assessment_step.html", {
        "session": session, "form": form, "step": step, "total_steps": total_steps,
        "progress_pct": progress_pct, "legend": mark_safe(legend_html),
        "category": questions[0].category if questions else None,
        "is_htmx": _is_htmx(request),
    })

def results(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
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
    for q in Question.objects.all():
        r = Response.objects.filter(session=session, question=q).first()
        if r:
            all_rows.append({
                "question": q,
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

    # -----------------------------
    # 🧩 Tool Recommendations Logic
    # -----------------------------
    recommendations = []
    for w in weakest_questions:
        q_text = (w["question"].text or "").lower()
        q_note = (w.get("note") or "").lower()
        cat = w["question"].category
        for m in ToolRecommendation.objects.filter(category=cat):
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
            # Get tools from this category not already in recommendations
            fallback_tools = ToolRecommendation.objects.filter(
                category=cat
            ).exclude(id__in=[r.id for r in recommendations])[:2]
            
            for tool in fallback_tools:
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
    for q_data in weakest_questions:
        if not q_data.get("ai_insight"):
            try:
                # Get the response object
                response = Response.objects.filter(
                    session=session,
                    question=q_data["question"]
                ).first()
                if response and response.score <= 2:
                    generate_diagnostic_insight(response)
                    # Refresh the insight
                    response.refresh_from_db()
                    q_data["ai_insight"] = response.ai_insight
            except Exception as e:
                log_error("Diagnostic Insight Generation", e, {"qid": q_data["question"].id_code})
    
    # -----------------------------
    # 🤖 Trigger AI playbook generation immediately (blocking for now)
    # -----------------------------
    if snap and not (snap.ai_playbook or "").strip():
        try:
            ai_md = generate_playbook_with_gemini(snap)
            if ai_md and ai_md.strip():
                snap.ai_playbook = ai_md.strip()
                snap.save(update_fields=["ai_playbook"])
        except Exception as e:
            log_error("AI Playbook Generation", e, {"session_id": str(session.uuid)})
            # Continue rendering even if AI fails

    return render(request, "gtm/results.html", {
        "session": session,
        "overall": round(overall, 1),
        "band": band,
        "band_actions_html": band_actions_html,
        "cat_scores": cat_scores,
        "labels": labels,
        "values": values,
        "strengths_categories": strengths_categories,
        "focus_categories": focus_categories,
        "weakest_questions": weakest_questions,
        "recommendations": recommendations,
        "is_htmx": _is_htmx(request),
    })

# Playbook
def playbook(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)
    cat_sorted = sorted(cat_scores, key=lambda x: x["avg"])

    # -----------------------------
    # 🧩 Format Recommended Next Moves (using centralized formatter)
    # -----------------------------
    band_actions_html = _format_band_actions_markdown(
        getattr(band, "actions_markdown", "") if band else ""
    )

    # -----------------------------
    # 🤖 AI Playbook Rendering (+ optional lazy-generate)
    # -----------------------------
    # Ensure we actually have a snapshot even if user skips Results page
    snap = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()

    # (Optional) Generate if empty - blocking to ensure it's ready when user arrives
    if snap and not (snap.ai_playbook or "").strip():
        try:
            ai_md_src = generate_playbook_with_gemini(snap)
            if ai_md_src and ai_md_src.strip():
                snap.ai_playbook = ai_md_src.strip()
                snap.save(update_fields=["ai_playbook"])
        except Exception as e:
            log_error("AI Playbook (lazy) Generation", e, {"session_id": str(session.uuid)})

    ai_playbook_html = ""
    try:
        if snap and getattr(snap, "ai_playbook", ""):
            # ✅ Normalize Gemini’s mixed formatting BEFORE Markdown
            src = snap.ai_playbook.replace("\r\n", "\n").strip()

            # 1️⃣ Convert inline " * " separators into proper bullet lines
            src = re.sub(r"\s\*\s+", "\n- ", src)

            # 2️⃣ Make "Week X:" style lines into Markdown headings for consistency
            src = re.sub(r"(?m)^(Week\s+\d+:[^\n]*)$", r"### \1", src)

            # 3️⃣ Add blank lines before list, numbered, and heading items for proper block rendering
            src = re.sub(r"(?m)(?<!\n)\n(?=(?:- |\d+\. |#{1,6}\s))", "\n\n", src)

            # 4️⃣ Clean up extra spaces/newlines
            src = re.sub(r"[ \t]+\n", "\n", src)
            src = re.sub(r"\n{3,}", "\n\n", src)

            # ✅ Render clean Markdown
            ai_playbook_html = md.markdown(
                src,
                extensions=["extra", "sane_lists", "toc"]  # 'extra' already includes tables
            )
        else:
            ai_playbook_html = ""
    except Exception as e:
        log_error("AI Playbook rendering", e, {"session_id": str(session.uuid)})
        ai_playbook_html = "<p class='text-red-600'>⚠️ Could not render AI playbook content. Check logs.</p>"

    # -----------------------------
    # Render Template
    # -----------------------------
    return render(request, "gtm/playbook.html", {
        "session": session,
        "overall": round(overall, 1),
        "band": band,
        "cat_scores": cat_scores,
        "cat_sorted": cat_sorted,
        "band_actions_html": band_actions_html,
        "ai_playbook_html": mark_safe(ai_playbook_html),
        "is_htmx": _is_htmx(request),
    })


def download_report_pdf(request, session_id):
    session = get_object_or_404(AssessmentSession, pk=session_id)
    cat_scores, overall = _compute_scores(session)
    band = _band_for_score(overall)
    return render_gtm_report_pdf_response(session=session, cat_scores=cat_scores, overall=overall, band=band)

def history(request):
    
    if request.user.is_authenticated:
        qs = AssessmentSession.objects.filter(user=request.user)
    else:
        cid = _client_id(request)
        # 🚨 IMPROVEMENT: Fetch snapshot and band info in one go
        qs = AssessmentSession.objects.filter(owner_client_id=cid).order_by("-created_at")
        
    # Use select_related to minimize queries
    qs = qs.select_related('snapshot__band')
    
    rows = []
    for s in qs:
        # 🚨 IMPROVEMENT: Use snapshot data if available (for completed sessions)
        snap = getattr(s, 'snapshot', None)
        if snap:
            overall = snap.overall
            band = snap.band
        elif s.is_completed:
            # Fallback for completed sessions without a snapshot (rare)
            cat_scores, overall = _compute_scores(s)
            band = _band_for_score(overall)
        else:
            overall = None
            band = None
            
        rows.append({"session": s, "overall": (round(overall,1) if overall is not None else None), "band": band})
        
    # Remove the old loop that called _compute_scores(s) if you use the above logic.
    # The original loop in history was:
    # for s in qs:
    #     cat_scores, overall = _compute_scores(s) if s.is_completed else ([], None)
    #     band = _band_for_score(overall) if overall is not None else None
    #     rows.append({"session": s, "overall": (round(overall,1) if overall is not None else None), "band": band})

    return render(request, "gtm/history.html", {"rows": rows, "is_htmx": _is_htmx(request)})


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
            note=note
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
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from .ai_chat import process_chat_message, get_suggested_prompts
from .models import ChatMessage

def chat_view(request, session_id):
    """Render the chat interface page"""
    session = get_object_or_404(AssessmentSession, pk=session_id)
    
    # Get chat history
    chat_history = ChatMessage.objects.filter(session=session).order_by('created_at')[:50]
    
    # Get suggested prompts
    suggested = get_suggested_prompts(session)
    
    return render(request, "gtm/chat.html", {
        "session": session,
        "chat_history": chat_history,
        "suggested_prompts": suggested,
        "is_htmx": _is_htmx(request),
    })

@require_http_methods(["POST"])
def chat_api(request, session_id):
    """API endpoint for chat messages"""
    import json
    
    try:
        session = get_object_or_404(AssessmentSession, pk=session_id)
        
        # Parse JSON body
        data = json.loads(request.body)
        message = data.get("message", "").strip()
        
        if not message:
            return JsonResponse({
                "success": False,
                "error": "Message cannot be empty"
            }, status=400)
        
        # Process the message
        result = process_chat_message(
            session_id=str(session_id),
            message=message,
            user=request.user if request.user.is_authenticated else None
        )
        
        # Save to database
        if result.get("success"):
            ChatMessage.objects.create(
                session=session,
                user=request.user if request.user.is_authenticated else None,
                message=message,
                response=result.get("response", ""),
                intent=result.get("intent", "")
            )
        
        return JsonResponse(result)
        
    except json.JSONDecodeError:
        return JsonResponse({
            "success": False,
            "error": "Invalid JSON"
        }, status=400)
    except Exception as e:
        log_error("Chat API Error", e, {"session_id": str(session_id)})
        return JsonResponse({
            "success": False,
            "error": "An error occurred processing your message"
        }, status=500)

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