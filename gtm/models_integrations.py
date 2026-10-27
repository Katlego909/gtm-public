# gtm/models_integrations.py
"""
Workspace-level connections to external services (CRM, etc.). A single small,
provider-agnostic model rather than a per-provider table -- credentials are
stored as an encrypted JSON blob so a future OAuth provider (refresh tokens,
etc.) doesn't require a schema change, only provider-specific shaping in
set_credential/get_credential.
"""
import hashlib
import json
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone

from .integrations.crypto import CredentialDecryptionError, decrypt_text, encrypt_text
from .models_workspace import Workspace


class WorkspaceIntegration(models.Model):
    PROVIDER_CHOICES = [
        ("hubspot", "HubSpot (CRM)"),
    ]
    AUTH_METHOD_CHOICES = [
        ("api_key", "API Key / Token"),
        ("oauth", "OAuth"),
    ]
    STATUS_CHOICES = [
        ("not_connected", "Not Connected"),
        ("connected", "Connected"),
        ("error", "Error"),
    ]

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="integrations")
    provider = models.CharField(max_length=20, choices=PROVIDER_CHOICES)
    auth_method = models.CharField(max_length=20, choices=AUTH_METHOD_CHOICES, default="api_key")

    encrypted_credentials = models.TextField(blank=True, default="")
    token_preview = models.CharField(max_length=8, blank=True, default="", help_text="Last 4 chars, for display only")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="not_connected")
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")

    configured_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="configured_integrations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ["workspace", "provider"]

    def __str__(self):
        return f"{self.workspace.name} - {self.get_provider_display()} ({self.status})"

    def set_credential(self, plaintext_token: str) -> None:
        """Encrypt and store `plaintext_token`. Caller must still call save()."""
        self.encrypted_credentials = encrypt_text(json.dumps({"token": plaintext_token}))
        self.token_preview = plaintext_token[-4:] if len(plaintext_token) >= 4 else plaintext_token

    def get_credential(self) -> str:
        """Decrypt and return the stored token. Raises CredentialDecryptionError on failure."""
        if not self.encrypted_credentials:
            raise CredentialDecryptionError("No credential stored for this integration.")
        data = json.loads(decrypt_text(self.encrypted_credentials))
        return data["token"]

    def clear_credential(self) -> None:
        self.encrypted_credentials = ""
        self.token_preview = ""


class WorkspaceApiKey(models.Model):
    """An inbound key an external tool (the customer's CRM, BI job, Zapier,
    etc.) uses to push measured gap-metric values into one workspace.

    The opposite direction from WorkspaceIntegration: that model holds
    credentials *we* send elsewhere, so they must be decryptable; an inbound
    key only ever needs to be checked, so only its SHA-256 hash is stored and
    the plaintext is shown once at creation.
    """
    KEY_PREFIX = "gtmk_"
    PREFIX_DISPLAY_LENGTH = 12

    workspace = models.ForeignKey(Workspace, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=80)
    prefix = models.CharField(max_length=PREFIX_DISPLAY_LENGTH, help_text="Leading characters, for display only")
    key_hash = models.CharField(max_length=64, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_api_keys",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.workspace.name} - {self.name} ({self.prefix}...)"

    @staticmethod
    def hash_key(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    @classmethod
    def generate(cls, workspace, name, user):
        """Create a key and return (instance, plaintext). The plaintext is
        not recoverable afterwards."""
        plaintext = cls.KEY_PREFIX + secrets.token_urlsafe(32)
        instance = cls.objects.create(
            workspace=workspace,
            name=name,
            prefix=plaintext[:cls.PREFIX_DISPLAY_LENGTH],
            key_hash=cls.hash_key(plaintext),
            created_by=user,
        )
        return instance, plaintext

    @classmethod
    def authenticate(cls, plaintext):
        """Return the live key matching `plaintext`, or None if it's unknown,
        revoked, or its workspace is no longer active."""
        if not plaintext or not plaintext.startswith(cls.KEY_PREFIX):
            return None
        api_key = cls.objects.select_related("workspace").filter(
            key_hash=cls.hash_key(plaintext),
            revoked_at__isnull=True,
            workspace__is_active=True,
        ).first()
        if api_key:
            api_key.last_used_at = timezone.now()
            api_key.save(update_fields=["last_used_at"])
        return api_key

    @property
    def is_active(self):
        return self.revoked_at is None
