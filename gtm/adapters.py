# gtm/adapters.py
"""
ACCOUNT_ADAPTER (see gtm_validator/settings.py) -- makes outbound mail a
best-effort side effect of authentication rather than a hard dependency of it.

allauth's DefaultAccountAdapter.send_mail calls msg.send() with no fail_silently
and no try/except, so an SMTP problem -- bad credentials, a rejected From
address, a provider throttle, a network blip -- propagates out of the view and
Django returns a bare 500. That is how signup broke in production: the user row
and the invite-code redemption both committed, then the verification email threw.

Signup no longer sends mail at all (ACCOUNT_EMAIL_VERIFICATION = "none"), but
password reset still does, and would fail the same way. Swallowing the error is
the right trade there: the reset view already refuses to reveal whether an
address exists, so the user-visible response is identical either way, and a
logged error beats a 500.
"""
from allauth.account.adapter import DefaultAccountAdapter

from .utils_logging import log_error


class ForgeAccountAdapter(DefaultAccountAdapter):
    def send_mail(self, template_prefix, email, context):
        try:
            super().send_mail(template_prefix, email, context)
        except Exception as exc:  # noqa: BLE001 - auth must survive a mail outage
            log_error(
                "Auth email send failed",
                exc,
                {"template": template_prefix, "to": email},
            )
