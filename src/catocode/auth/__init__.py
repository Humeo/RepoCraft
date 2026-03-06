"""Auth module — factory and public exports."""

from __future__ import annotations

import os

from .base import Auth
from .github_app import GitHubAppAuth
from .token import TokenAuth

__all__ = ["Auth", "TokenAuth", "GitHubAppAuth", "get_auth"]


def get_auth() -> Auth:
    """Return the appropriate Auth implementation based on environment variables.

    Priority:
      1. GitHub App  — when GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY +
                        GITHUB_APP_INSTALLATION_ID are all set.
      2. Token Auth  — when GITHUB_TOKEN is set.

    Raises:
        RuntimeError: if neither set of credentials is available.
    """
    app_id = os.environ.get("GITHUB_APP_ID")
    private_key = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n")
    installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID")

    if app_id and private_key and installation_id:
        return GitHubAppAuth(app_id, private_key, installation_id)

    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return TokenAuth(token)

    raise RuntimeError(
        "No GitHub credentials found.\n"
        "  Option A (personal token): set GITHUB_TOKEN\n"
        "  Option B (GitHub App):     set GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, "
        "GITHUB_APP_INSTALLATION_ID"
    )
