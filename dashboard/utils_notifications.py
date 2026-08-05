from .models import Notification, UserSettings
from django.contrib.auth import get_user_model

User = get_user_model()

# Maps a Notification.notification_type to the UserSettings fields that gate
# in-app creation and email delivery for it. Types with no entry (e.g. 'system')
# are always created in-app and never emailed.
PREFERENCE_MAP = {
    'task': ('inapp_task_assigned', 'email_task_assigned'),
    'task_status': ('inapp_task_completed', 'email_task_completed'),
    'invite': ('inapp_workspace_activity', 'email_workspace_invite'),
    'ai_report': ('inapp_ai_insights', 'email_ai_insights'),
}


def send_notification(recipient, title, message, notification_type='system', level='info', sender=None, workspace=None, link=""):
    """
    Centralized helper to send a notification to a specific user.

    Respects the recipient's UserSettings in-app/email toggles for the given
    notification_type. Creating the in-app row and sending the email are each
    gated independently, so a user can get one channel without the other.
    """
    if not recipient or not recipient.is_authenticated:
        return None

    inapp_field, email_field = PREFERENCE_MAP.get(notification_type, (None, None))

    settings, _ = UserSettings.objects.get_or_create(user=recipient)

    notification = None
    if inapp_field is None or getattr(settings, inapp_field):
        notification = Notification.objects.create(
            recipient=recipient,
            sender=sender,
            workspace=workspace,
            notification_type=notification_type,
            level=level,
            title=title,
            message=message,
            link=link
        )

    if email_field and getattr(settings, email_field):
        from gtm.utils_email import send_notification_email
        try:
            send_notification_email(recipient, title, message, link)
        except Exception as e:
            from gtm.utils_logging import log_error
            log_error("Notification Email Send Failed", e, {
                "recipient": recipient.username,
                "notification_type": notification_type,
            })

    return notification
