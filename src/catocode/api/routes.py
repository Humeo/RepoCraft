"""Protected /api/* routes — all require a valid session."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..store import Store
from .deps import CurrentUser

logger = logging.getLogger(__name__)

router = APIRouter(tags=["api"])


class PatrolSettings(BaseModel):
    patrol_enabled: bool
    patrol_interval_hours: int = 12
    patrol_max_issues: int = 5
    patrol_window_hours: int = 12


def _get_store_from_app(router_instance: APIRouter) -> Store:
    """Placeholder — store is injected via request.app.state in the factory."""
    raise NotImplementedError


def _enrich_activity(activity: dict) -> dict:
    """Add computed pipeline_stage field to an activity dict."""
    a = dict(activity)
    status = a.get("status", "pending")
    if status == "pending" and a.get("requires_approval"):
        a["pipeline_stage"] = "pending_approval"
    else:
        a["pipeline_stage"] = status
    return a


def make_router(store: Store) -> APIRouter:
    """Return an APIRouter with the store injected via closure."""
    r = APIRouter(tags=["api"])

    @r.get("/me")
    async def get_me(current_user: CurrentUser) -> dict:
        return {
            "id": current_user["id"],
            "github_login": current_user["github_login"],
            "github_email": current_user["github_email"],
            "avatar_url": current_user["avatar_url"],
            "created_at": current_user["created_at"],
            "last_login_at": current_user["last_login_at"],
        }

    @r.get("/stats")
    async def get_stats(current_user: CurrentUser) -> dict:
        return store.get_stats(user_id=current_user["id"])

    @r.get("/repos")
    async def list_repos(current_user: CurrentUser) -> list[dict]:
        return [dict(r) for r in store.list_repos(user_id=current_user["id"])]

    @r.get("/repos/{repo_id}")
    async def get_repo_stats(repo_id: str, current_user: CurrentUser) -> dict:
        stats = store.get_repo_stats(repo_id)
        if stats is None:
            raise HTTPException(status_code=404, detail="Repo not found")
        # Ownership check
        if stats["repo"].get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")
        return stats

    @r.get("/repos/{repo_id}/activities")
    async def list_repo_activities(repo_id: str, current_user: CurrentUser) -> list[dict]:
        repo = store.get_repo(repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="Repo not found")
        if repo.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")
        return [_enrich_activity(a) for a in store.list_activities(repo_id=repo_id)]

    @r.get("/activities")
    async def list_activities(current_user: CurrentUser) -> list[dict]:
        return [_enrich_activity(a) for a in store.list_activities(user_id=current_user["id"])]

    @r.get("/activities/{activity_id}")
    async def get_activity(activity_id: str, current_user: CurrentUser) -> dict:
        activity = store.get_activity(activity_id)
        if activity is None:
            raise HTTPException(status_code=404, detail="Activity not found")
        repo = store.get_repo(activity["repo_id"])
        if repo is None or repo.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")
        return _enrich_activity(activity)

    @r.get("/activities/{activity_id}/logs")
    async def get_activity_logs(activity_id: str, current_user: CurrentUser) -> list[dict]:
        activity = store.get_activity(activity_id)
        if activity is None:
            raise HTTPException(status_code=404, detail="Activity not found")
        repo = store.get_repo(activity["repo_id"])
        if repo is None or repo.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")
        return [dict(log) for log in store.get_logs(activity_id)]

    @r.get("/activities/{activity_id}/logs/stream")
    async def stream_activity_logs(activity_id: str, current_user: CurrentUser):
        """SSE endpoint — streams new log lines in real-time."""
        activity = store.get_activity(activity_id)
        if activity is None:
            raise HTTPException(status_code=404, detail="Activity not found")
        repo = store.get_repo(activity["repo_id"])
        if repo is None or repo.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")

        async def event_generator():
            last_id = 0
            while True:
                new_logs = store.get_logs_after(activity_id, last_id)
                for log in new_logs:
                    last_id = log["id"]
                    yield {"data": json.dumps(dict(log))}

                # Check if activity is finished
                current = store.get_activity(activity_id)
                if current and current["status"] in ("done", "failed"):
                    yield {
                        "event": "status",
                        "data": json.dumps({"status": current["status"]}),
                    }
                    return

                await asyncio.sleep(1)

        return EventSourceResponse(event_generator())

    @r.delete("/repos/{repo_id}")
    async def delete_repo(repo_id: str, current_user: CurrentUser) -> dict:
        repo = store.get_repo(repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="Repo not found")
        if repo.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")
        store.delete_repo(repo_id)
        logger.info("User %s deleted repo %s", current_user["id"][:8], repo_id)
        return {"status": "deleted"}

    @r.get("/install-url")
    async def get_install_url(current_user: CurrentUser) -> dict:
        from ..config import get_github_app_name
        app_name = get_github_app_name()
        url = (
            f"https://github.com/apps/{app_name}/installations/new"
            f"?state={current_user['id']}"
        )
        return {"url": url}

    # --- Patrol endpoints ---

    @r.patch("/repos/{repo_id}/patrol")
    async def update_patrol_settings(
        repo_id: str, settings: PatrolSettings, current_user: CurrentUser
    ) -> dict:
        repo = store.get_repo(repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="Repo not found")
        if repo.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")
        store.update_patrol_settings(
            repo_id=repo_id,
            enabled=settings.patrol_enabled,
            interval_hours=settings.patrol_interval_hours,
            max_issues=settings.patrol_max_issues,
            window_hours=settings.patrol_window_hours,
        )
        logger.info(
            "User %s updated patrol settings for %s: enabled=%s",
            current_user["id"][:8],
            repo_id,
            settings.patrol_enabled,
        )
        return {"status": "updated"}

    @r.post("/repos/{repo_id}/patrol/trigger")
    async def trigger_patrol(repo_id: str, current_user: CurrentUser) -> dict:
        repo = store.get_repo(repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="Repo not found")
        if repo.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")

        # Ensure budget row exists for this repo
        max_issues = repo.get("patrol_max_issues") or 5
        window_hours = repo.get("patrol_window_hours") or 12
        store.init_patrol_budget(repo_id, max_issues=max_issues, window_hours=window_hours)

        budget = store.get_patrol_budget(repo_id)
        if budget <= 0:
            raise HTTPException(
                status_code=429,
                detail=f"Patrol budget exhausted ({max_issues} issues/{window_hours}h window). Wait for the window to reset.",
            )

        activity_id = store.add_activity(repo_id, "patrol", f"budget:{budget}")
        logger.info("Manual patrol trigger by %s for %s (budget=%d)", current_user["id"][:8], repo_id, budget)
        return {"status": "triggered", "activity_id": activity_id}

    @r.get("/repos/{repo_id}/patrol/status")
    async def get_patrol_status(repo_id: str, current_user: CurrentUser) -> dict:
        repo = store.get_repo(repo_id)
        if repo is None:
            raise HTTPException(status_code=404, detail="Repo not found")
        if repo.get("user_id") != current_user["id"]:
            raise HTTPException(status_code=403, detail="Access denied")

        budget = store.get_patrol_budget(repo_id)

        # Find last patrol activity
        activities = store.list_activities(repo_id=repo_id)
        last_patrol_at: str | None = None
        for a in reversed(list(activities)):
            if a["kind"] == "patrol" and a["status"] in ("done", "failed"):
                last_patrol_at = a["updated_at"]
                break

        # Check embedding service
        from ..embeddings import check_embedding_service, is_embedding_service_configured
        if is_embedding_service_configured():
            emb_ok, emb_err = await check_embedding_service()
            embedding_status = "ok" if emb_ok else f"error: {emb_err}"
        else:
            embedding_status = "not_configured"

        return {
            "enabled": bool(repo.get("patrol_enabled")),
            "patrol_interval_hours": repo.get("patrol_interval_hours", 12),
            "patrol_max_issues": repo.get("patrol_max_issues", 5),
            "patrol_window_hours": repo.get("patrol_window_hours", 12),
            "budget_remaining": budget,
            "last_patrol_at": last_patrol_at,
            "last_patrol_sha": repo.get("last_patrol_sha"),
            "embedding_service_status": embedding_status,
        }

    return r
