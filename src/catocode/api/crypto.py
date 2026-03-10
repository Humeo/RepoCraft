"""Fernet-based encryption for GitHub access tokens stored in the database."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet

from ..config import get_session_secret_key

# PBKDF2 salt — fixed and public (the security comes from SESSION_SECRET_KEY entropy)
_SALT = b"catocode-token-encryption-v1"
_ITERATIONS = 100_000


def _get_fernet() -> Fernet:
    """Derive a Fernet key from SESSION_SECRET_KEY using PBKDF2."""
    raw = get_session_secret_key().encode()
    key_bytes = hashlib.pbkdf2_hmac("sha256", raw, _SALT, _ITERATIONS, dklen=32)
    key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(key)


def encrypt_token(token: str) -> str:
    """Encrypt a GitHub access token for storage."""
    return _get_fernet().encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    """Decrypt a stored GitHub access token."""
    return _get_fernet().decrypt(encrypted.encode()).decode()
