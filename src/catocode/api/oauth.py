"""GitHub OAuth 2.0 flow routes."""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..config import (
    get_base_url,
    get_frontend_url,
    get_github_oauth_client_id,
    get_github_oauth_client_secret,
)
from ..store import Store
from .crypto import encrypt_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SESSION_MAX_AGE = 30 * 86400  # 30 days
OAUTH_SCOPE = "read:user user:email"


def _get_store(request: Request) -> Store:
    return request.app.state.store


@router.get("/github")
async def github_login(request: Request) -> RedirectResponse:
    """Redirect to GitHub OAuth authorization page."""
    store = _get_store(request)
    state = secrets.token_hex(32)
    store.create_oauth_state(state)

    client_id = get_github_oauth_client_id()
    base_url = get_base_url()
    redirect_uri = f"{base_url}/auth/github/callback"

    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope={OAUTH_SCOPE}"
        f"&state={state}"
    )
    return RedirectResponse(url)


@router.get("/github/callback")
async def github_callback(code: str, state: str, request: Request) -> RedirectResponse:
    """Handle GitHub OAuth callback, create session, redirect to frontend."""
    store = _get_store(request)
    frontend_url = get_frontend_url()

    # Validate CSRF state
    if not store.consume_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")

    # Exchange code for access token
    client_id = get_github_oauth_client_id()
    client_secret = get_github_oauth_client_secret()

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": client_id, "client_secret": client_secret, "code": code},
            headers={"Accept": "application/json"},
            timeout=15.0,
        )

    if token_resp.status_code != 200:
        logger.error("GitHub token exchange failed: %s", token_resp.text)
        raise HTTPException(status_code=502, detail="GitHub token exchange failed")

    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="No access token in GitHub response")

    # Fetch authenticated user info
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=10.0,
        )

    if user_resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to fetch GitHub user")

    gh_user = user_resp.json()
    github_id: int = gh_user["id"]
    github_login: str = gh_user["login"]
    avatar_url: str | None = gh_user.get("avatar_url")
    github_email: str | None = gh_user.get("email")

    # Upsert user
    encrypted_token = encrypt_token(access_token)
    existing = store.get_user_by_github_id(github_id)
    if existing:
        user_id = existing["id"]
        store.update_user_last_login(user_id, encrypted_token)
        logger.info("User %s logged in (existing)", github_login)
    else:
        user_id = str(uuid.uuid4())
        store.create_user(
            user_id=user_id,
            github_id=github_id,
            github_login=github_login,
            github_email=github_email,
            avatar_url=avatar_url,
            access_token=encrypted_token,
        )
        logger.info("User %s created (new)", github_login)

    # Create session
    session_token = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=SESSION_MAX_AGE)).isoformat()
    store.create_session(session_token, user_id, expires_at)

    # Set httpOnly session cookie and redirect to dashboard
    response = RedirectResponse(url=f"{frontend_url}/dashboard")
    response.set_cookie(
        key="session",
        value=session_token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=os.environ.get("CATOCODE_BASE_URL", "").startswith("https"),
        samesite="lax",
    )
    return response


@router.get("/github/install-callback")
async def github_install_callback(
    installation_id: str,
    state: str | None = None,
    request: Request = None,
) -> RedirectResponse:
    """GitHub App installation callback — link installation to user and redirect to dashboard."""
    store = _get_store(request)
    frontend_url = get_frontend_url()

    # Validate the CSRF state token to retrieve the associated user_id
    if state:
        user_id = store.consume_install_state(state)
        if user_id:
            store.link_installation_to_user(installation_id, user_id)
            logger.info("Linked installation %s to user %s", installation_id, user_id[:8])
        else:
            logger.warning(
                "install-callback: invalid or expired state token (installation_id=%s)",
                installation_id,
            )

    return RedirectResponse(url=f"{frontend_url}/dashboard")


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Delete session and clear cookie."""
    store = _get_store(request)
    session_cookie = request.cookies.get("session")
    if session_cookie:
        store.delete_session(session_cookie)

    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie("session")
    return response
