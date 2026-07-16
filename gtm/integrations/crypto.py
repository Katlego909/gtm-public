"""Encrypt/decrypt helpers for workspace-integration credentials at rest.

Keyed from settings.INTEGRATION_ENCRYPTION_KEY (see gtm_validator/settings.py
for how that key is sourced). Deliberately a separate key from Django's
SECRET_KEY so rotating SECRET_KEY doesn't also corrupt every stored token.
"""
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class CredentialDecryptionError(Exception):
    """Raised when stored credential ciphertext can't be decrypted (tampered or key rotated)."""


@lru_cache(maxsize=1)
def _get_fernet() -> Fernet:
    return Fernet(settings.INTEGRATION_ENCRYPTION_KEY.encode("utf-8"))


def encrypt_text(plaintext: str) -> str:
    """Encrypt `plaintext`, returning a ciphertext string safe to store in a TextField."""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_text(ciphertext: str) -> str:
    """Decrypt a string produced by encrypt_text(). Raises CredentialDecryptionError on failure."""
    try:
        return _get_fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        raise CredentialDecryptionError("Could not decrypt stored credential.") from exc
