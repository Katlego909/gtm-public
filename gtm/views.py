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
from django.conf import settings
from functools import wraps
from .utils import transfer_firmographics_to_snapshot
from .ai_services import generate_playbook_with_gemini, generate_diagnostic_insight, _normalize_ai_playbook_markdown
from .forms import StartAssessmentForm # Added import

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

def _log_access_denied(request, reason, session_id=None, details=None):
    """Log denied access attempts for audit trail."""
    user = request.user.username if request.user.is_authenticated else "anonymous"
    client_id = _client_id(request)
    log_details = {
        "reason": reason,
        "user": user,
        "client_id": client_id,
        "session_id": str(session_id) if session_id else None,
        **(details or {})
    }
    log_error("Access Denied", Exception(reason), log_details)


def safe_get_session_or_403(request, session_id):
    """
    Safely retrieve a session with comprehensive ownership & workspace checks.
    - Only authenticated users are allowed.
    - Authenticated users can access owned sessions.
    - Workspace members can access sessions in their workspace.
    
    Returns: (session, is_authorized)
    """
    from gtm.models_workspace import WorkspaceMembership
    
    try:
        session = AssessmentSession.objects.get(pk=session_id)
    except AssessmentSession.DoesNotExist:
        return None, False
    
    if not request.user.is_authenticated:
        _log_access_denied(request, "Anonymous access denied", session_id)
        return None, False
    
    # Authenticated user: check ownership first (always allowed)
    if session.user == request.user:
        return session, True
    
    # If session has a workspace, check if user is a member
    if session.workspace:
        try:
            membership = WorkspaceMembership.objects.get(
                user=request.user,
                workspace=session.workspace,
                is_active=True
            )
            # User is a member and can access workspace sessions
            return session, True
        except WorkspaceMembership.DoesNotExist:
            _log_access_denied(
                request,
                "User not in session's workspace",
                session_id,
                {"workspace_id": str(session.workspace.id)}
            )
            return None, False
    
    # No workspace: can only be accessed by owner
    _log_access_denied(request, "User is not the session owner", session_id)
    return None, False


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
    # Prefetch related questions to avoid N+1 queries when accessing cat.questions.all()
    categories = Category.objects.all().order_by("id").prefetch_related('questions')
    return [list(cat.questions.all().order_by("id"))
            for cat in categories]
    
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
        snap, created = ResultSnapshot.objects.update_or_create(
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
            }
        )
        # Transfer firmographics using the helper
        transfer_firmographics_to_snapshot(session, snap)
        snap.save(update_fields=[
            "company_name", "industry", "website", "contact_name",
            "contact_email", "contact_role", "phone", "company_size",
            "revenue_range", "country", "crm", "utm_source",
            "utm_medium", "utm_campaign", "referrer"
        ]) # Save changes made by transfer_firmographics_to_snapshot
    return snap
        
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
            widget=forms.Textarea(attrs={"class": "w-full border rounded px-3 py-2 mt-4", "rows": 3, "placeholder": "Please provide more info on this"}),
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

    return render(request, "gtm/assessment_step.html", {
        "session": session, "form": form, "step": step, "total_steps": total_steps,
        "progress_pct": progress_pct, "legend": mark_safe(legend_html),
        "category": questions[0].category if questions else None,
        "is_htmx": _is_htmx(request),
        "context_fields": context_fields,
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
    for q in Question.objects.all():
        r = Response.objects.filter(session=session, question=q).first()
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
    for w in weakest_questions:
        q_text = (w["question"].text or "").lower()
        q_note = (w.get("note") or "").lower()
        
        # Filter through the prefetched recommendations for the current question's category
        # and then apply the keyword matching logic
        for m in [tr for tr in all_tool_recommendations if tr.category.id == w["question"].category.id]:
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
        # (This logic was already good and will remain)
        weak_categories = []
        for w in weakest_questions:
            cat = w["question"].category
            if cat not in weak_categories:
                weak_categories.append(cat)
        
        # Add generic tools from each weak category until we have at least 5
        # (Optimization: Filter fallback_tools from all_tool_recommendations if possible
        #  or make this query outside the loop if it's called often)
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
    # Prefetch all relevant responses for weakest questions to avoid N+1
    weakest_question_ids = [q_data["question"].id for q_data in weakest_questions]
    
    # Fetch responses and map question_id to response manually as question_id is not unique for in_bulk()
    responses_qs = Response.objects.filter(
        session=session,
        question__id__in=weakest_question_ids
    ).select_related('question') # select_related to avoid N+1 when accessing response.question later
    
    responses_by_question_id = {r.question.id: r for r in responses_qs}

    for q_data in weakest_questions:
        # Use prefetched response if available
        response = responses_by_question_id.get(q_data["question"].id)
        
        if response and response.score <= 2:
            if not response.ai_insight:
                # 🤖 TRIGGER ASYNC GENERATION
                try:
                    from threading import Thread
                    # Capture response ID to avoid closure issues
                    rid = response.id
                    def gen_diagnostic_async(resp_id):
                        try:
                            from .models import Response
                            from .ai_services import generate_diagnostic_insight
                            r = Response.objects.get(id=resp_id)
                            generate_diagnostic_insight(r)
                        except Exception as e:
                            log_error("Async Diagnostic Gen", e)
                    
                    Thread(target=gen_diagnostic_async, args=(rid,), daemon=True).start()
                    q_data["ai_insight_loading"] = True
                except Exception as e:
                    log_error("Diagnostic Thread creation", e)
            else:
                q_data["ai_insight"] = response.ai_insight

    
    # -----------------------------
    # 🤖 Trigger AI playbook generation in background (non-blocking)
    # -----------------------------
    if snap and not (snap.ai_playbook or "").strip():
        try:
            # Import here to avoid circular import issues  
            from threading import Thread
            
            def generate_async():
                try:
                    ai_md = generate_playbook_with_gemini(snap)
                    if ai_md and ai_md.strip():
                        snap.ai_playbook = ai_md.strip()
                        snap.save(update_fields=["ai_playbook"])
                except Exception as e:
                    log_error("AI Playbook Generation (async)", e, {"session_id": str(session.uuid)})
            
            # Start generation in background thread
            thread = Thread(target=generate_async)
            thread.daemon = True
            thread.start()
        except Exception as e:
            log_error("AI Playbook thread creation (results)", e, {"session_id": str(session.uuid)})

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
        "snap": snap,
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
    
    # Still generating. Return the loading state.
    return render(request, "gtm/partials/playbook_loading_button.html", {"session": session})


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

    if playbook_ready:
        src = _normalize_ai_playbook_markdown(snap.ai_playbook)
        ai_playbook_html = md.markdown(src, extensions=["extra", "sane_lists", "toc"])
        return render(request, "gtm/partials/playbook_content_status.html", {
            "session": session,
            "playbook_ready": True,
            "playbook_loading": False,
            "ai_playbook_html": mark_safe(ai_playbook_html),
        })

    # Ensure generation remains non-blocking while polling.
    if snap:
        try:
            from threading import Thread

            def generate_async():
                try:
                    ai_md_src = generate_playbook_with_gemini(snap)
                    if ai_md_src and ai_md_src.strip():
                        snap.ai_playbook = ai_md_src.strip()
                        snap.save(update_fields=["ai_playbook"])
                except Exception as e:
                    log_error("AI Playbook (async) Poll Generation", e, {"session_id": str(session.uuid)})

            thread = Thread(target=generate_async)
            thread.daemon = True
            thread.start()
        except Exception as e:
            log_error("AI Playbook poll thread creation", e, {"session_id": str(session.uuid)})

    return render(request, "gtm/partials/playbook_content_status.html", {
        "session": session,
        "playbook_ready": False,
        "playbook_loading": bool(snap),
        "ai_playbook_html": "",
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

    # -----------------------------
    # 🤖 AI Playbook Rendering (+ optional lazy-generate)
    # -----------------------------
    # Ensure we actually have a snapshot even if user skips Results page
    snap = getattr(session, "snapshot", None) or ResultSnapshot.objects.filter(session=session).first()

    # (Optional) Generate if empty - NON-BLOCKING to prevent page hangs
    needs_generation = snap and not (snap.ai_playbook or "").strip()
    
    # Trigger background generation if needed (non-blocking)
    if needs_generation:
        try:
            # Import here to avoid circular import issues  
            from threading import Thread
            
            def generate_async():
                try:
                    ai_md_src = generate_playbook_with_gemini(snap)
                    if ai_md_src and ai_md_src.strip():
                        snap.ai_playbook = ai_md_src.strip()
                        snap.save(update_fields=["ai_playbook"])
                except Exception as e:
                    log_error("AI Playbook (async) Generation", e, {"session_id": str(session.uuid)})
            
            # Start generation in background thread
            thread = Thread(target=generate_async)
            thread.daemon = True
            thread.start()
        except Exception as e:
            log_error("AI Playbook thread creation", e, {"session_id": str(session.uuid)})
    
    playbook_ready = False
    playbook_loading = bool(needs_generation)
    ai_playbook_html = ""
    try:
        if snap and getattr(snap, "ai_playbook", ""):
            src = _normalize_ai_playbook_markdown(snap.ai_playbook)
            ai_playbook_html = md.markdown(
                src,
                extensions=["extra", "sane_lists", "toc"]  # 'extra' already includes tables
            )
            playbook_ready = True
    except Exception as e:
        log_error("AI Playbook rendering", e, {"session_id": str(session.uuid)})
        ai_playbook_html = ""
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
        "ai_playbook_html": mark_safe(ai_playbook_html),
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

    if response.score <= 2 and not (response.ai_insight or "").strip():
        try:
            from threading import Thread

            def gen_diagnostic_async(resp_id):
                try:
                    from .models import Response
                    from .ai_services import generate_diagnostic_insight
                    r = Response.objects.get(id=resp_id)
                    generate_diagnostic_insight(r)
                except Exception as e:
                    log_error("Async Diagnostic Gen (poll)", e)

            Thread(target=gen_diagnostic_async, args=(response.id,), daemon=True).start()
        except Exception as e:
            log_error("Diagnostic poll thread creation", e)

    # Refresh model state after potential async kickoff fallback writes.
    response.refresh_from_db(fields=["ai_insight"])

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
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from .ai_chat import process_chat_message, get_suggested_prompts
from .models import ChatMessage

def chat_view(request, session_id):
    """Render the chat interface page"""
    # Access control: ensure user owns or is in session's workspace
    session, is_authorized = safe_get_session_or_403(request, session_id)
    if not is_authorized:
        from django.http import HttpResponseForbidden
        return HttpResponseForbidden("Access denied to this session.")
    
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
        # Access control: ensure user owns or is in session's workspace
        session, is_authorized = safe_get_session_or_403(request, session_id)
        if not is_authorized:
            return JsonResponse({
                "success": False,
                "error": "Access denied to this session."
            }, status=403)

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