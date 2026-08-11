# gtm/models_beta.py
"""
Invite-code gate for the closed beta. Signup is otherwise fully open
(django-allauth defaults, no custom adapter) -- a BetaInviteCode row is the
only thing standing between a stranger and a working account while the
beta is invite-only. One row per invited tester; single-use once `used_by`
is set (see gtm/forms_beta.py's signup form mixin for the consuming side).
"""
import secrets

from django.conf import settings
from django.db import models


def _generate_code() -> str:
    return secrets.token_urlsafe(6).replace("_", "").replace("-", "").upper()[:8]


class BetaInviteCode(models.Model):
    code = models.CharField(max_length=16, unique=True, default=_generate_code, db_index=True)
    email = models.EmailField(
        blank=True, default="",
        help_text="Who this code was generated for -- used as the send target for "
                  "the 'Email invite code' admin action. Not enforced at signup: the "
                  "code itself is the gate, not a match against this address.",
    )
    note = models.CharField(max_length=200, blank=True, default="", help_text="e.g. \"Jane @ Acme\"")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="beta_codes_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the invite email was last sent to the address above. Blank = "
                  "never sent. Doubles as the send-once latch: saving a code in the "
                  "admin only auto-sends while this is blank (or when `email` changes).",
    )
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Blank = never expires.")
    used_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="beta_code_used",
    )
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        status = "used" if self.used_by_id else "unused"
        return f"{self.code} ({status}){' — ' + self.note if self.note else ''}"

    @property
    def is_used(self) -> bool:
        return self.used_by_id is not None

    @property
    def is_expired(self) -> bool:
        from django.utils import timezone
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def is_sent(self) -> bool:
        return self.sent_at is not None

    def send_invite_email(self, request=None):
        """Email this code to `self.email` and record the send.

        Sending and stamping live together here so the three callers -- the admin
        save, the admin resend action, and generate_beta_invite_codes -- cannot
        drift on what "sent" means.

        Deliberately does not swallow exceptions: `sent_at` stays untouched when
        SMTP fails, so the send is retryable and callers decide how to report the
        failure (the admin turns it into a message rather than a 500).
        """
        from django.utils import timezone
        from .utils_email import send_beta_invite_email

        send_beta_invite_email(self, self.email, request=request)
        self.sent_at = timezone.now()
        self.save(update_fields=["sent_at"])
