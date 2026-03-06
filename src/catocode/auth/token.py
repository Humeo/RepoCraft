"""Personal access token auth (simple GITHUB_TOKEN)."""

from __future__ import annotations

from .base import Auth


class TokenAuth(Auth):
    """Auth using a static GitHub personal access token."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def get_token(self) -> str:
        return self._token

    def auth_type(self) -> str:
        return "token"
