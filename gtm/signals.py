# gtm/signals.py
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from .models import ResultSnapshot
from .utils_email import send_snapshot_report_email

User = get_user_model()


@receiver(post_save, sender=User, dispatch_uid="gtm.auto_join_invited_workspaces")
def auto_join_invited_workspaces(sender, instance, created, raw=False, update_fields=None, **kwargs):
    """
    When a user registers (or is invited while already registered), create
    memberships for any *pending, unexpired* workspace invitations addressed to
    their email.

    Guarded against two failure modes:
    - Runs are skipped for fixture loads (``raw``) and for ``last_login``-only
      saves (emitted on every login by ``update_last_login``). Without this, a
      removed member would be silently re-added on their next login.
    - Only pending, unexpired invitations are honored, so stale/expired invites
      never grant access (matches the expiry check in ``workspace_join``).
    """
    if raw:
        return
    # Skip the last_login-only save Django emits on every successful login.
    if update_fields is not None and set(update_fields) <= {"last_login"}:
        return
    if not instance.email:
        return

    from django.utils import timezone
    from .models_workspace import WorkspaceInvitation, WorkspaceMembership

    invitations = WorkspaceInvitation.objects.filter(
        email__iexact=instance.email,
        is_accepted=False,
        accepted_at__isnull=True,
        expires_at__gt=timezone.now(),
    )
    from dashboard.utils_notifications import send_notification

    for invite in invitations:
        if not WorkspaceMembership.objects.filter(workspace=invite.workspace, user=instance).exists():
            WorkspaceMembership.objects.create(
                workspace=invite.workspace,
                user=instance,
                role=invite.role,
                invited_by=invite.invited_by,
            )
            # Mark invitation as accepted
            invite.accepted_at = timezone.now()
            invite.is_accepted = True
            invite.save(update_fields=['accepted_at', 'is_accepted'])
            send_notification(
                recipient=instance,
                sender=invite.invited_by,
                workspace=invite.workspace,
                notification_type='system',
                level='success',
                title="Welcome to the team",
                message=f"You've automatically joined {invite.workspace.name} from a pending invitation.",
                link=f"/dashboard/?workspace={invite.workspace.id}"
            )


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
    if not instance.session.is_completed:
        return

    # Atomically claim the send: the conditional UPDATE flips report_sent to True
    # only if it is currently False, and returns the number of rows changed. This
    # guards against the duplicate sends caused by the snapshot being save()d
    # multiple times during playbook generation (the in-memory report_sent stays
    # False across those saves), and is race-safe across workers/threads.
    # A queryset update also avoids re-triggering this post_save signal.
    claimed = type(instance).objects.filter(pk=instance.pk, report_sent=False).update(report_sent=True)
    if not claimed:
        return

    try:
        send_snapshot_report_email(instance)
        instance.report_sent = True  # keep the in-memory instance consistent
    except Exception as e:
        # Release the claim so a later save can retry, and log without crashing
        # the assessment flow.
        type(instance).objects.filter(pk=instance.pk).update(report_sent=False)
        from .utils_logging import log_error
        log_error("Email Report Send Failed", e, {
            "session_id": str(instance.session.uuid),
            "snapshot_id": instance.pk
        })
