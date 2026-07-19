from .models import AssessmentSession, ResultSnapshot
import datetime
import mimetypes
from django.utils import timezone
from django.conf import settings

def _client_id(request):
    """Extract client ID from cookie for anonymous user tracking."""
    return request.COOKIES.get("gtm_client", "")

def guess_upload_mime_type(filename: str) -> str:
    """Best-effort MIME type for a stored upload's Gemini Part.from_bytes()
    call. Extension-based since GTMFile/Resource don't persist a
    content-type at upload time."""
    guessed, _ = mimetypes.guess_type(filename or "")
    return (guessed or "application/octet-stream").lower()

def transfer_firmographics_to_snapshot(session: AssessmentSession, snapshot: ResultSnapshot):
    """
    Transfers firmographic data from an AssessmentSession to a ResultSnapshot instance.
    This consolidates the logic for copying fields for denormalization.
    """
    firmographic_fields = [
        "company_name", "industry", "website", "contact_name",
        "contact_email", "contact_role", "phone", "company_size",
        "revenue_range", "country", "crm", "company_stage", "utm_source",
        "utm_medium", "utm_campaign", "referrer"
    ]
    
    for field_name in firmographic_fields:
        setattr(snapshot, field_name, getattr(session, field_name, "") or "")

def get_existing_incomplete_session(client_id: str) -> AssessmentSession | None:
    """Returns the most recent incomplete AssessmentSession for a given client_id."""
    return AssessmentSession.objects.filter(
        owner_client_id=client_id, is_completed=False
    ).order_by("-created_at").first()

def check_daily_assessment_cap(client_id: str) -> bool:
    """
    Checks if the client has reached the daily assessment cap.
    Returns True if cap is reached, False otherwise.
    """
    MAX_ASSESSMENTS_PER_DAY = getattr(settings, "MAX_ASSESSMENTS_PER_DAY", 3)
    today = timezone.now().date()
    daily_count = AssessmentSession.objects.filter(
        owner_client_id=client_id,
        created_at__date=today
    ).count()
    return daily_count >= MAX_ASSESSMENTS_PER_DAY

def check_assessment_cooldown(client_id: str) -> tuple[bool, int]:
    """
    Checks if the client is within the cooldown period between new assessments.
    Returns (True, minutes_left) if in cooldown, (False, 0) otherwise.
    """
    MIN_SECONDS_BETWEEN_ASSESSMENTS = getattr(settings, "MIN_SECONDS_BETWEEN_ASSESSMENTS", 5 * 60)
    last_session = AssessmentSession.objects.filter(
        owner_client_id=client_id
    ).order_by("-created_at").first()
    
    if last_session:
        seconds_since_last = (timezone.now() - last_session.created_at).total_seconds()
        if seconds_since_last < MIN_SECONDS_BETWEEN_ASSESSMENTS:
            wait_left = int(MIN_SECONDS_BETWEEN_ASSESSMENTS - seconds_since_last)
            minutes_left = max(1, wait_left // 60)
            return True, minutes_left
    return False, 0