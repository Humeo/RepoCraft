"""GitHub App installation token auth.

Flow:
  1. Sign a short-lived JWT with the App's private key
  2. Exchange JWT for an installation access token (valid ~1 hour)
  3. Cache the installation token; refresh before expiry
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
import jwt

from .base import Auth

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
# Refresh 60 s before expiry to avoid using a token that's about to expire
REFRESH_BUFFER_SECS = 60


class GitHubAppAuth(Auth):
    """Auth using a GitHub App installation token."""

    def __init__(
        self,
        app_id: str,
        private_key_pem: str,
        installation_id: str,
    ) -> None:
        """
        Args:
            app_id: GitHub App ID (numeric string).
            private_key_pem: PEM-encoded RSA private key downloaded from GitHub.
            installation_id: Installation ID for the target account/org.
        """
        self._app_id = app_id
        self._private_key = private_key_pem
        self._installation_id = installation_id

        self._cached_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _make_jwt(self) -> str:
        """Create a signed JWT for authenticating as the GitHub App itself."""
        now = int(time.time())
        payload = {
            "iat": now - 60,   # Allow 60 s clock drift
            "exp": now + 600,  # JWT valid for 10 minutes (GitHub max)
            "iss": self._app_id,
        }
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def _fetch_installation_token(self) -> tuple[str, float]:
        """Fetch a new installation access token from GitHub.

        Returns (token, expires_at_timestamp).
        """
        app_jwt = self._make_jwt()
        url = f"{GITHUB_API}/app/installations/{self._installation_id}/access_tokens"
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, headers=headers)
            response.raise_for_status()
            data = response.json()

        token: str = data["token"]
        expires_at_str: str = data["expires_at"]  # e.g. "2024-01-01T00:10:00Z"
        expires_at_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
        expires_at_ts = expires_at_dt.timestamp()
        logger.debug(
            "GitHub App installation token obtained, expires at %s", expires_at_str
        )
        return token, expires_at_ts

    async def get_token(self) -> str:
        """Return a valid installation token, refreshing if necessary."""
        async with self._lock:
            if self._cached_token and time.time() < self._expires_at - REFRESH_BUFFER_SECS:
                return self._cached_token

            logger.debug(
                "Refreshing GitHub App installation token for installation %s",
                self._installation_id,
            )
            self._cached_token, self._expires_at = await self._fetch_installation_token()
            return self._cached_token

    def auth_type(self) -> str:
        return "github_app"
