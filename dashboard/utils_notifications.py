from .models import Notification
from django.contrib.auth import get_user_model

User = get_user_model()

def send_notification(recipient, title, message, notification_type='system', level='info', sender=None, workspace=None, link=""):
    """
    Centralized helper to send a notification to a specific user.
    """
    if not recipient or not recipient.is_authenticated:
        return None
        
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
    return notification
