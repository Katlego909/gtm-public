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
        from ..models import GTMFile
        gtm_file = GTMFile.objects.create(
            session=session,
            file=uploaded_file,
            file_type=file_type
        )
        
        # TRIGGER ASYNC INITIAL AUDIT
        # This caches the result so the chat agent doesn't have to wait for a 10s API call
        # Build context for the audit
        cat_scores, overall = _compute_scores(session)
        band = _band_for_score(overall)
        context_str = f"Company GTM Score: {overall}/100. Stage: {band.stage if band else 'N/A'}"

        def run_initial_audit(file_id, ctx):
            from ..models import GTMFile
            from ..ai_auditor import perform_gtm_visual_audit
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
        from ..delivery_analyzer import extract_text_from_path
        from ..ai_services import _get_client
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
    from ..delivery_analyzer import analyze_delivery_documents as _run_analysis

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
        from ..delivery_analyzer import extract_text_from_path
        from ..ai_services import _get_client
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

    from ..category_analyzer import analyze_category_documents as _run_analysis
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


