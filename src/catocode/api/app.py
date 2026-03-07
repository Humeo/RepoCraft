"""FastAPI application factory for SaaS mode."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..auth import Auth, get_auth
from ..config import get_frontend_url
from ..store import Store
from . import deps as _deps
from .oauth import router as oauth_router
from .routes import make_router as make_api_router


def create_app(store: Store, auth: Auth | None = None) -> FastAPI:
    """Build and return the unified FastAPI application.

    Serves:
    - /auth/*           — GitHub OAuth flow
    - /api/*            — Protected REST API (session-guarded)
    - /webhook/github/* — Per-repo webhooks
    - /webhook/app      — GitHub App-level webhook
    - /webhook/health   — Health check
    - /health           — Top-level health check
    """
    from ..webhook.server import WebhookServer

    app = FastAPI(title="CatoCode SaaS API")

    # Store on app.state so route handlers can access it via request.app.state.store
    app.state.store = store

    # Inject store into session-dependency module
    _deps.set_store(store)

    frontend_url = get_frontend_url()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[frontend_url, "http://localhost:3000", "http://localhost:3001"],
        allow_credentials=True,  # required for cookie-based sessions
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # OAuth flow (unauthenticated)
    app.include_router(oauth_router)

    # Protected API routes
    api_router = make_api_router(store)
    app.include_router(api_router, prefix="/api")

    # Webhook server (includes /webhook/* routes + legacy dashboard at /)
    _auth = auth or get_auth()
    webhook_server = WebhookServer(store=store, auth=_auth)
    app.mount("/", webhook_server.app)

    return app
