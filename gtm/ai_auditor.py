"""
gtm/ai_auditor.py

Strategic evidence auditor using Gemini multimodal capabilities.
Supports auditing both GTMFile objects (session evidence) and
dashboard.Resource objects (workspace-level strategy assets).
"""
import logging
import re
import threading
from django.conf import settings
from django.utils import timezone

try:
    from google import genai
    from google.genai import types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    
logger = logging.getLogger(__name__)


# ─── Score extraction ─────────────────────────────────────────────────────────

def _extract_score_modifier(audit_text: str) -> int:
    """
    Parses the AI's audit text looking for an 'Evidence Score' (1-10) and
    converts it into a GTM score modifier in the range -5 to +5.
    
    Evidence Score 1-3 → negative modifier (-1 to -3)
    Evidence Score 4-6 → neutral modifier (0)
    Evidence Score 7-10 → positive modifier (+1 to +3)
    """
    if not audit_text:
        return 0

    # Look for "Evidence Score: N" or "Evidence Score: N/10"
    match = re.search(
        r"evidence\s+score[:\s*]+(\d+(?:\.\d+)?)\s*(?:/\s*10)?",
        audit_text,
        re.IGNORECASE
    )
    if not match:
        return 0
    
    try:
        score = float(match.group(1))
        if score <= 3:
            return -3
        elif score <= 4:
            return -2
        elif score <= 5:
            return -1
        elif score <= 6:
            return 0
        elif score <= 7:
            return 1
        elif score <= 8:
            return 2
        elif score <= 9:
            return 3
        else:
            return 3  # max cap
    except (ValueError, TypeError):
        return 0


def _extract_gtm_categories(audit_text: str) -> list:
    """
    Looks for known GTM category names mentioned in the audit text
    and returns them as a list for tagging the resource.
    """
    known = [
        "Lead Generation", "Sales Efficiency", "Customer Success",
        "Product Marketing", "Sales Velocity", "Marketing ROI",
    ]
    found = []
    for cat in known:
        if cat.lower() in audit_text.lower():
            found.append(cat)
    return found


def _extract_summary(audit_text: str, max_len: int = 300) -> str:
    """Extract the first meaningful paragraph as a short summary."""
    if not audit_text:
        return ""
    lines = [l.strip() for l in audit_text.split('\n') if l.strip() and not l.strip().startswith('#')]
    summary = " ".join(lines[:3])
    return summary[:max_len] + ("..." if len(summary) > max_len else "")


# ─── Core audit function ──────────────────────────────────────────────────────

def _run_multimodal_audit(file_bytes: bytes, mime_type: str, asset_name: str, session_context: str = "") -> str:
    """
    Core multimodal audit. Sends file bytes + prompt to Gemini and returns raw text.
    """
    project_id = getattr(settings, "GCP_PROJECT_ID", None)
    location = getattr(settings, "GCP_LOCATION", "europe-west1")

    if not GENAI_AVAILABLE or not project_id:
        logger.error("google-genai not available or GCP_PROJECT_ID missing for audit.")
        return ""

    client = genai.Client(vertexai=True, project=project_id, location=location)

    audit_prompt = f"""You are the 'GTM Strategic Auditor'. 
Perform a professional strategic critique of this {asset_name}.

CONTEXT FROM GTM ASSESSMENT:
{session_context or 'Standard GTM strategic planning context.'}

CRITIQUE CATEGORIES:
1. **Messaging Clarity**: Is the core Value Proposition obvious within 3 seconds?
2. **Conversion Friction**: Are there barriers to user action or a confusing CTA?
3. **Strategic Alignment**: Does this asset solve the gaps identified in the company's GTM scores?
4. **Professionalism & Trust**: Does the design/structure inspire confidence in the target segment?

REPORT FORMAT:
- Use clear headers for each category above.
- Provide 3–5 specific 'Power Moves' for improvement in a final section.
- End with a single line: **Evidence Score: N/10** (where N is your overall rating).
- Also mention any of these GTM categories if relevant: Lead Generation, Sales Efficiency, Customer Success, Product Marketing, Sales Velocity, Marketing ROI.
"""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                        types.Part.from_text(text=audit_prompt),
                    ]
                )
            ]
        )
        return response.text or ""
    except Exception as e:
        logger.error(f"Multimodal Audit API call failed: {e}")
        return ""


# ─── GTMFile audit (legacy / session evidence) ───────────────────────────────

def perform_gtm_visual_audit(session_file, session_context=None):
    """
    Audits a GTMFile object (attached to an AssessmentSession).
    Saves results back to the model.
    """
    try:
        session_file.file.open('rb')
        file_bytes = session_file.file.read()
        session_file.file.close()

        filename = session_file.file.name.lower()
        if filename.endswith(".pdf"):
            mime_type = "application/pdf"
        elif filename.endswith(".png"):
            mime_type = "image/png"
        elif filename.endswith((".jpg", ".jpeg")):
            mime_type = "image/jpeg"
        else:
            mime_type = "application/octet-stream"

    except Exception as e:
        logger.error(f"Failed to read GTMFile for audit: {e}")
        return "ERROR: Could not read the evidence file."

    asset_name = session_file.get_file_type_display()
    audit_text = _run_multimodal_audit(file_bytes, mime_type, asset_name, session_context or "")
    
    if audit_text:
        session_file.ai_audit_notes = audit_text
        session_file.ai_audit_score_modifier = _extract_score_modifier(audit_text)
        session_file.audit_status = 'complete'
    else:
        session_file.audit_status = 'failed'
    
    session_file.save()
    return audit_text or "Audit could not be completed."


# ─── Resource audit (Asset Library) ──────────────────────────────────────────

def perform_resource_audit(resource) -> bool:
    """
    Audits a dashboard.Resource object (workspace-level strategic asset).
    Only works for 'file' type resources.
    Saves results back to the resource model.
    Returns True on success.
    """
    if resource.resource_type != 'file' or not resource.file:
        logger.warning(f"Resource {resource.id} is not a file — skipping audit.")
        resource.audit_status = 'failed'
        resource.save(update_fields=['audit_status'])
        return False

    try:
        resource.file.open('rb')
        file_bytes = resource.file.read()
        resource.file.close()

        filename = resource.file.name.lower()
        if filename.endswith(".pdf"):
            mime_type = "application/pdf"
        elif filename.endswith(".png"):
            mime_type = "image/png"
        elif filename.endswith((".jpg", ".jpeg")):
            mime_type = "image/jpeg"
        else:
            mime_type = "application/octet-stream"

    except Exception as e:
        logger.error(f"Failed to read Resource file for audit: {resource.id} — {e}")
        resource.audit_status = 'failed'
        resource.save(update_fields=['audit_status'])
        return False

    # Build a rich context string from the resource metadata
    session_context = (
        f"Asset Name: {resource.name}\n"
        f"Category: {resource.get_category_display()}\n"
        f"Description: {resource.description or 'N/A'}\n"
        f"Workspace: {resource.workspace.name if resource.workspace else 'Unknown'}\n"
    )
    
    asset_name = resource.get_category_display()
    audit_text = _run_multimodal_audit(file_bytes, mime_type, asset_name, session_context)

    from django.utils import timezone
    if audit_text:
        resource.ai_audit_summary = _extract_summary(audit_text)
        resource.ai_score_modifier = _extract_score_modifier(audit_text)
        resource.gtm_categories = _extract_gtm_categories(audit_text)
        resource.ai_audit_at = timezone.now()
        resource.audit_status = 'complete'
        # Store full notes in a separate field if needed (re-use description temporarily)
        # We store the full text in ai_audit_summary at max_len=300, 
        # and full text is accessible via a separate detail endpoint.
        # To avoid a schema change, we'll store full text in a hidden JSON metadata approach:
        # Actually let's just store the full thing in ai_audit_summary without truncation
        resource.ai_audit_summary = audit_text  # full text; UI will truncate display
        resource.save(update_fields=[
            'ai_audit_summary', 'ai_score_modifier', 'gtm_categories',
            'ai_audit_at', 'audit_status'
        ])
        return True
    else:
        resource.audit_status = 'failed'
        resource.ai_audit_at = timezone.now()
        resource.save(update_fields=['audit_status', 'ai_audit_at'])
        return False


def audit_resource_async(resource_id):
    """
    Background-thread-safe wrapper for perform_resource_audit.
    Fetches the resource from DB fresh (avoids thread-local Django ORM issues),
    marks it as 'auditing', runs the audit, then saves the result.
    """
    def _run():
        try:
            # Import inside thread to avoid circular import issues
            from dashboard.models import Resource
            resource = Resource.objects.get(pk=resource_id)
            resource.audit_status = 'auditing'
            resource.save(update_fields=['audit_status'])
            perform_resource_audit(resource)
        except Exception as e:
            logger.error(f"audit_resource_async failed for {resource_id}: {e}")
            try:
                from dashboard.models import Resource
                Resource.objects.filter(pk=resource_id).update(audit_status='failed')
            except Exception:
                pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def audit_strategic_evidence_async(gtm_file_id):
    """Background-thread-safe wrapper for performing a GTMFile audit."""
    def _run():
        try:
            from gtm.models import GTMFile
            session_file = GTMFile.objects.get(pk=gtm_file_id)
            session_file.audit_status = 'auditing'
            session_file.save(update_fields=['audit_status'])
            
            # Get context from session if available
            context = None
            if session_file.session:
                context = (
                    f"Company: {session_file.session.company_name or 'Unknown'}\n"
                    f"Industry: {session_file.session.industry or 'Unknown'}\n"
                )
            perform_gtm_visual_audit(session_file, session_context=context)
        except Exception as e:
            logger.error(f"audit_strategic_evidence_async failed for {gtm_file_id}: {e}")
            try:
                from gtm.models import GTMFile
                GTMFile.objects.filter(pk=gtm_file_id).update(audit_status='failed')
            except Exception:
                pass

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread
