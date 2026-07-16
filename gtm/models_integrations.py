# gtm/models_integrations.py
"""
Workspace-level connections to external services (CRM, etc.). A single small,
provider-agnostic model rather than a per-provider table -- credentials are
stored as an encrypted JSON blob so a future OAuth provider (refresh tokens,
etc.) doesn't require a schema change, only provider-specific shaping in
set_credential/get_credential.
"""
import json

from django.conf import settings
from django.db import models

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
