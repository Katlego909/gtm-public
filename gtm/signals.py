# gtm/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from .models import ResultSnapshot
from .utils_email import send_snapshot_report_email

User = get_user_model()


@receiver(post_save, sender=User)
def auto_join_invited_workspaces(sender, instance, created, **kwargs):
    """
    When a new user registers (or an existing user updates their profile),
    check if they have any pending/accepted workspace invitations and
    automatically create memberships.
    """
    if not instance.email:
        return
    from .models_workspace import WorkspaceInvitation, WorkspaceMembership
    invitations = WorkspaceInvitation.objects.filter(email__iexact=instance.email)
    for invite in invitations:
        if not WorkspaceMembership.objects.filter(workspace=invite.workspace, user=instance).exists():
            WorkspaceMembership.objects.create(
                workspace=invite.workspace,
                user=instance,
                role=invite.role,
                invited_by=invite.invited_by,
            )
            # Mark invitation as accepted
            if not invite.accepted_at:
                from django.utils import timezone
                invite.accepted_at = timezone.now()
                invite.is_accepted = True
                invite.save(update_fields=['accepted_at', 'is_accepted'])


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
