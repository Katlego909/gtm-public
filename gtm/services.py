import re
import math
from django.db import transaction
from django.utils import timezone
from django.core.cache import cache
from django.utils.html import strip_tags
from django.utils.safestring import mark_safe
from django.db.models import Sum, F
import markdown as md

from .models import Category, Question, AssessmentSession, Response, RecommendationBand, ResultSnapshot
from .utils_logging import log_error
from .utils import transfer_firmographics_to_snapshot, _client_id
from .ai_services import generate_playbook_with_gemini

def _expand_gtm_jargon(text: str) -> str:
    """Replace common GTM acronyms with plain-language expansions for non-specialist users."""
    expanded = text or ""
    replacements = {
        "ICP": "Ideal Customer Profile (ICP)",
        "SLA": "service-level agreement (SLA)",
        "CAC": "customer acquisition cost (CAC)",
        "QBR": "quarterly business review (QBR)",
        "TTV": "time to value (TTV)",
        "CRM": "customer relationship management system (CRM)",
    }
    for short, full in replacements.items():
        expanded = re.sub(rf"\b{re.escape(short)}\b", full, expanded)
    return expanded


def _build_question_guidance(question: Question) -> dict:
    """Create user-friendly guidance shown under each assessment question."""
    metadata = question.ai_metadata if isinstance(question.ai_metadata, dict) else {}
    evidence_type = (metadata.get("evidence_type") or "qualitative").lower()
    owner_role = (metadata.get("owner_role") or "team").lower()
    time_horizon = (metadata.get("time_horizon") or "current").lower()
    dimension = (metadata.get("dimension") or "").lower()
    q_text = (question.text or "").lower()

    evidence_hint_map = {
        "system-data": "Use system data: CRM, dashboards, or tracked metrics.",
        "quantitative": "Use recent numbers, not guesses.",
        "qualitative": "Use team knowledge and written process notes.",
    }
    role_hint_map = {
        "marketing": "Ask marketing or demand generation.",
        "sales": "Ask sales managers or reps.",
        "cs": "Ask customer success or account managers.",
        "revops": "Ask RevOps or data owners.",
        "founder": "Ask leadership for strategy context.",
    }

    horizon_hint_map = {
        "current": "Score your process as it is today.",
        "30d": "Use data from the last 30 days.",
        "90d": "Use data from the last 90 days.",
        "12m": "Use trend data from the last 12 months.",
    }

    quick_help = f"{horizon_hint_map.get(time_horizon, horizon_hint_map['current'])} Score what is true, not ideal."

    # Keep examples specific to the type of question so users can mirror the format.
    if "icp" in dimension or "ideal customer" in q_text:
        example_note = "We updated our ICP and now require industry, company size, and buyer role on each lead."
    elif "attribution" in dimension or "utm" in q_text or "source" in q_text:
        example_note = "Only 62% of leads have full source tags; paid social and referrals are often marked direct."
    elif "speed to lead" in dimension or "response" in q_text:
        example_note = "Median first response is 9 hours for web leads and 2 days for email leads; no SLA alerts yet."
    elif "qualification" in dimension or "qualif" in q_text:
        example_note = "Reps use different qualification rules; only budget and timeline are captured consistently."
    elif "pipeline" in dimension or "stage" in q_text:
        example_note = "Stage definitions are unclear, so opportunities move forward without clear exit rules."
    elif "win-loss" in dimension or "win" in q_text or "loss" in q_text:
        example_note = "Closed-lost reasons are mostly free text, so we cannot track top loss patterns clearly."
    elif "time to value" in dimension or "ttv" in q_text:
        example_note = "Time to first value averages 28 days and varies by segment because onboarding is inconsistent."
    elif "retention" in dimension or "renew" in q_text:
        example_note = "We review renewals quarterly, but churn reasons are not tracked by segment, so actions are reactive."
    elif "health" in dimension or "customer health" in q_text:
        example_note = "We do not have a formal health score; risk is identified manually from support tickets and low usage."
    elif "advocacy" in dimension or "testimonial" in q_text or "review" in q_text:
        example_note = "We request testimonials informally, so only a few quotes were captured last quarter."
    else:
        example_note = "Our process exists but is inconsistent across teams, and this metric is not reviewed regularly."

    return {
        "plain_question": _expand_gtm_jargon(question.text),
        "quick_help": quick_help,
        "why_this_matters": _expand_gtm_jargon(question.diagnostic_note) or "This shows where execution is blocking growth.",
        "how_to_answer": evidence_hint_map.get(evidence_type, evidence_hint_map["qualitative"]),
        "who_to_ask": role_hint_map.get(owner_role, "Ask the teammate closest to this process."),
        "example_note": example_note,
    }


def _kickoff_playbook_generation(snapshot, session_id=None):
    """Kick off non-blocking playbook generation once, guarded against rapid duplicate starts."""
    if not snapshot:
        return False
    if (snapshot.ai_playbook or "").strip():
        return False

    # Skip if already generating, done, or failed
    snap_status = getattr(snapshot, "ai_playbook_status", "pending")
    if snap_status in ("generating", "done", "failed"):
        return False

    kickoff_key = f"gtm:playbook:kickoff:{snapshot.id}"
    # Throttle kickoff frequency across concurrent polling requests.
    if not cache.add(kickoff_key, "1", timeout=20):
        return False

    try:
        from threading import Thread

        def generate_async(snapshot_id, sid):
            try:
                fresh_snapshot = ResultSnapshot.objects.filter(id=snapshot_id).first()
                if not fresh_snapshot or (fresh_snapshot.ai_playbook or "").strip():
                    return
                generate_playbook_with_gemini(fresh_snapshot)
            except Exception as e:
                log_error("AI Playbook async kickoff", e, {"session_id": str(sid) if sid else ""})

        thread = Thread(target=generate_async, args=(snapshot.id, session_id), daemon=True)
        thread.start()
        return True
    except Exception as e:
        log_error("AI Playbook thread creation", e, {"session_id": str(session_id) if session_id else ""})
        return False


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
        
        # Update the structure with the calculated average and percentage
        scores_by_id[cat_id].update({"avg": avg, "pct": (avg / 5.0) * 100.0})
        
        # Calculate overall contribution
        weight = cat_weight_map.get(cat_id, 0)
        overall += (avg / 5.0) * (weight / total_w) * 100.0
        
    # Convert the map back to a list of scores
    cat_scores = list(scores_by_id.values())

    return cat_scores, overall


def _band_for_score(score):
    return RecommendationBand.objects.filter(min_score__lte=score, max_score__gte=score).first()


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
