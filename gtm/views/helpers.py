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



