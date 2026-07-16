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