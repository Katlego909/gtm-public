# gtm/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import ResultSnapshot
from .utils_email import send_snapshot_report_email

@receiver(post_save, sender=ResultSnapshot)
def send_report_when_snapshot_saved(sender, instance: ResultSnapshot, created, **kwargs):
    """
    Send exactly once per snapshot unless you reset the flag.
    Guard with session.is_completed if you only want final results.
    """
    # Only send on create or when not previously sent
    if created or not getattr(instance, "report_sent", False):
        # Optional: only send when the assessment is completed
        if hasattr(instance.session, "is_completed") and not instance.session.is_completed:
            return
        send_snapshot_report_email(instance)
        type(instance).objects.filter(pk=instance.pk).update(report_sent=True)
