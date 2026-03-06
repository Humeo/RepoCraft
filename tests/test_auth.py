"""Tests for the auth module."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

import pytest

from catocode.auth import get_auth
from catocode.auth.token import TokenAuth
from catocode.auth.github_app import GitHubAppAuth


# --- TokenAuth ---

@pytest.mark.asyncio
async def test_token_auth_returns_token():
    auth = TokenAuth("ghp_test123")
    assert await auth.get_token() == "ghp_test123"


def test_token_auth_type():
    auth = TokenAuth("ghp_test123")
    assert auth.auth_type() == "token"


# --- get_auth factory ---

def test_get_auth_uses_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    auth = get_auth()
    assert isinstance(auth, TokenAuth)
    assert auth.auth_type() == "token"


def test_get_auth_uses_github_app(monkeypatch):
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    auth = get_auth()
    assert isinstance(auth, GitHubAppAuth)
    assert auth.auth_type() == "github_app"


def test_get_auth_prefers_app_over_token(monkeypatch):
    """GitHub App credentials take priority over GITHUB_TOKEN."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_abc")
    monkeypatch.setenv("GITHUB_APP_ID", "123456")
    monkeypatch.setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----")
    monkeypatch.setenv("GITHUB_APP_INSTALLATION_ID", "789")

    auth = get_auth()
    assert isinstance(auth, GitHubAppAuth)


def test_get_auth_raises_when_no_credentials(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("GITHUB_APP_INSTALLATION_ID", raising=False)

    with pytest.raises(RuntimeError, match="No GitHub credentials"):
        get_auth()


# --- GitHubAppAuth token caching ---

@pytest.mark.asyncio
async def test_github_app_caches_token(monkeypatch):
    """get_token() should return cached token without hitting the API again."""
    auth = GitHubAppAuth("123", "fake-key", "456")

    # Pre-populate the cache with a token that won't expire soon
    auth._cached_token = "ghs_cached"
    auth._expires_at = time.time() + 3600  # Valid for 1 hour

    token = await auth.get_token()
    assert token == "ghs_cached"


@pytest.mark.asyncio
async def test_github_app_refreshes_expired_token(monkeypatch):
    """get_token() should fetch a new token when cached one is about to expire."""
    auth = GitHubAppAuth("123", "fake-key", "456")

    # Simulate expired cache
    auth._cached_token = "ghs_old"
    auth._expires_at = time.time() + 30  # Expires in 30 s < REFRESH_BUFFER_SECS (60 s)

    new_token = "ghs_new"
    new_expires = "2099-01-01T00:00:00Z"

    mock_response = AsyncMock()
    mock_response.json = lambda: {"token": new_token, "expires_at": new_expires}
    mock_response.raise_for_status = lambda: None

    mock_post = AsyncMock(return_value=mock_response)

    with patch("catocode.auth.github_app.GitHubAppAuth._make_jwt", return_value="jwt"), \
         patch("httpx.AsyncClient.post", mock_post):
        token = await auth.get_token()

    assert token == new_token
    assert auth._cached_token == new_token
