from django.contrib import admin
from django.db import transaction
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse

from allauth.account.views import signup as allauth_signup


def health_check(request):
    """Health check endpoint for Cloud Run and container orchestration."""
    try:
        from django.db import connections
        db = connections['default']
        db.ensure_connection()
        return JsonResponse({'status': 'healthy'}, status=200)
    except Exception as e:
        return JsonResponse({'status': 'unhealthy', 'error': str(e)}, status=503)


urlpatterns = [
    path('health/', health_check, name='health_check'),
    path('admin/', admin.site.urls),
    # Same view and URL name allauth would install below, but run inside a
    # transaction. allauth commits signup in stages -- the user row first, then
    # the invite-code redemption (gtm/forms_beta.py), then login -- so anything
    # that raised later left a half-finished signup behind: the account existed
    # and the invite code was spent, and the retry hit "already been used".
    # Wrapping the view means a failure anywhere rolls the whole thing back and
    # the code stays claimable. Registered ahead of the include so it wins
    # resolution; both reverse to the same /accounts/signup/.
    path('accounts/signup/', transaction.atomic(allauth_signup), name='account_signup'),
    path('accounts/', include('allauth.urls')),
    path('', include('gtm.urls', namespace='gtm')),
    path('workspace/', include(('gtm.urls_workspace', 'workspace'), namespace='workspace')),
    path('dashboard/', include('dashboard.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
