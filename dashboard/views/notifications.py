from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods, require_POST

from dashboard.models import Notification

NOTIFICATION_DROPDOWN_LIMIT = 10


def _render_notification_dropdown(request):
    notifications = Notification.objects.filter(recipient=request.user)[:NOTIFICATION_DROPDOWN_LIMIT]
    unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()
    return render(request, 'dashboard/partials/notification_dropdown.html', {
        'notifications': notifications,
        'unread_count': unread_count,
    })


@require_http_methods(["GET"])
@login_required
def notification_dropdown(request):
    """Render the bell dropdown: recent notifications + unread count."""
    return _render_notification_dropdown(request)


@require_http_methods(["GET"])
@login_required
def notification_unread_count(request):
    """Tiny polling endpoint that renders just the bell badge."""
    unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()
    return render(request, 'dashboard/partials/notification_badge.html', {
        'unread_count': unread_count,
    })


@require_POST
@login_required
def notification_mark_read(request, pk):
    """Mark one notification read, then behave like a dropdown refresh."""
    notification = get_object_or_404(Notification, pk=pk, recipient=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=['is_read'])
    return _render_notification_dropdown(request)


@require_POST
@login_required
def notifications_mark_all_read(request):
    """Mark every notification for this user as read."""
    Notification.objects.filter(recipient=request.user, is_read=False).update(is_read=True)
    return _render_notification_dropdown(request)
