from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.http import JsonResponse


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
    path('accounts/', include('allauth.urls')),
    path('', include('gtm.urls', namespace='gtm')),
    path('workspace/', include(('gtm.urls_workspace', 'workspace'), namespace='workspace')),
    path("__reload__/", include("django_browser_reload.urls")),
    path('dashboard/', include('dashboard.urls')),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
