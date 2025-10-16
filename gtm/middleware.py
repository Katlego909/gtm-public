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
