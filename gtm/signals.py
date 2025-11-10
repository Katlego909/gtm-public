# gtm/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import ResultSnapshot
from .utils_email import send_snapshot_report_email

@receiver(post_save, sender=ResultSnapshot)
def send_report_when_snapshot_saved(sender, instance: ResultSnapshot, created, **kwargs):
    """
    Send email when:
    1. Assessment is completed (session.is_completed = True)
    2. Email hasn't been sent yet (report_sent = False)
    
    This handles both:
    - Snapshot created after assessment completion
    - Snapshot updated when assessment is marked complete
    """
    # Check if session is completed and email not yet sent
    if instance.session.is_completed and not instance.report_sent:
        try:
            send_snapshot_report_email(instance)
            # Use direct SQL update to avoid triggering this signal again
            type(instance).objects.filter(pk=instance.pk).update(report_sent=True)
        except Exception as e:
            # Log error but don't crash the assessment flow
            from .utils_logging import log_error
            log_error("Email Report Send Failed", e, {
                "session_id": str(instance.session.uuid),
                "snapshot_id": instance.pk
            })
