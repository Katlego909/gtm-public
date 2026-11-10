import uuid
from django.utils.deprecation import MiddlewareMixin

CLIENT_COOKIE = "gtm_client"
MAX_AGE = 60 * 60 * 24 * 365 * 2  # 2 years

class EnsureClientIdMiddleware(MiddlewareMixin):
    def process_response(self, request, response):
        if CLIENT_COOKIE not in request.COOKIES:
            cid = str(uuid.uuid4())
            response.set_cookie(CLIENT_COOKIE, cid, max_age=MAX_AGE, samesite="Lax")
        return response


ACTIVE_DAY_SESSION_KEY = "_active_day"


class RecordActiveDayMiddleware:
    """Record each day a signed-in user is active (feeds the funnel's
    "came back" step). The session remembers the last day written, so this
    costs one small INSERT per user per day, not a query per request.
    Must run after SessionMiddleware and AuthenticationMiddleware."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            from django.utils import timezone
            today = timezone.now().date()
            if request.session.get(ACTIVE_DAY_SESSION_KEY) != today.isoformat():
                from .models_beta import UserActiveDay
                # One INSERT ... ON CONFLICT DO NOTHING, not get_or_create's
                # SELECT + SAVEPOINT + INSERT + RELEASE.
                UserActiveDay.objects.bulk_create(
                    [UserActiveDay(user=user, day=today)], ignore_conflicts=True)
                request.session[ACTIVE_DAY_SESSION_KEY] = today.isoformat()
        return self.get_response(request)
