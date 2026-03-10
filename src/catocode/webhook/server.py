"""FastAPI webhook server for receiving GitHub events."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ..auth import Auth, get_auth
from ..config import get_github_app_webhook_secret, parse_repo_url, repo_id_from_url
from ..dashboard import dashboard_html_route
from ..dashboard import make_router as make_dashboard_router
from ..decision import decide_engagement
from ..store import Store
from .parser import parse_webhook
from .verifier import verify_signature

logger = logging.getLogger(__name__)


class WebhookServer:
    """FastAPI server for receiving GitHub webhooks."""

    def __init__(self, store: Store, auth: Auth | None = None) -> None:
        self._store = store
        self._auth = auth or get_auth()
        self.app = FastAPI(title="CatoCode Webhook Server")

        # Security check: warn if app webhook secret is not configured
        if not get_github_app_webhook_secret():
            logger.warning(
                "GITHUB_APP_WEBHOOK_SECRET is not set — "
                "the /webhook/app endpoint will accept unauthenticated requests. "
                "Set this variable in production to prevent spoofed webhook events."
            )

        # Dashboard UI + API
        self.app.get("/")(lambda: dashboard_html_route())
        self.app.include_router(make_dashboard_router(store))

        # Per-repo webhook (personal token mode or manual setup)
        self.app.post("/webhook/github/{repo_id}")(self._handle_webhook)
        # GitHub App-level webhook (all events from all installations)
        self.app.post("/webhook/app")(self._handle_app_webhook)
        self.app.get("/webhook/health")(self._health_check)

    async def _health_check(self) -> dict[str, str]:
        """Health check endpoint."""
        return {"status": "ok"}

    async def _handle_webhook(
        self,
        repo_id: str,
        request: Request,
        x_github_event: str = Header(...),
        x_github_delivery: str = Header(...),
        x_hub_signature_256: str | None = Header(None),
    ) -> Response:
        """Handle incoming GitHub webhook.

        Args:
            repo_id: Repository ID (owner-repo format)
            request: FastAPI request object
            x_github_event: Event type (issues, pull_request, etc.)
            x_github_delivery: Unique delivery ID
            x_hub_signature_256: HMAC signature for verification

        Returns:
            JSON response with status
        """
        # Get repository configuration
        repo = self._store.get_repo(repo_id)
        if repo is None:
            logger.warning("Webhook received for unknown repo: %s", repo_id)
            raise HTTPException(status_code=404, detail="Repository not found")

        # Get webhook secret (optional — if not configured, skip signature verification)
        webhook_config = self._store.get_webhook_config(repo_id)

        # Read raw body for signature verification
        body = await request.body()

        # Verify signature only if a secret is configured
        if webhook_config and webhook_config.get("webhook_secret"):
            secret = webhook_config["webhook_secret"]
            if x_hub_signature_256:
                if not verify_signature(body, x_hub_signature_256, secret):
                    logger.warning("Invalid webhook signature for repo: %s", repo_id)
                    raise HTTPException(status_code=401, detail="Invalid signature")
            else:
                logger.warning("Missing webhook signature for repo: %s", repo_id)
                raise HTTPException(status_code=401, detail="Missing signature")
        else:
            logger.debug("No webhook secret configured for %s, skipping signature check", repo_id)

        # Parse JSON payload
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.error("Invalid JSON payload for repo: %s", repo_id)
            raise HTTPException(status_code=400, detail="Invalid JSON")

        # Check for duplicate delivery
        if self._store.is_webhook_event_processed(x_github_delivery):
            logger.info("Duplicate webhook delivery: %s", x_github_delivery)
            return JSONResponse({"status": "duplicate", "event_id": x_github_delivery})

        # Store raw webhook event
        self._store.add_webhook_event(
            event_id=x_github_delivery,
            repo_id=repo_id,
            event_type=x_github_event,
            payload=json.dumps(payload),
        )

        # Parse webhook into normalized event
        event = parse_webhook(
            event_name=x_github_event,
            payload=payload,
            delivery_id=x_github_delivery,
            repo_id=repo_id,
        )

        if event is None:
            logger.debug("Webhook event ignored: %s for repo %s", x_github_event, repo_id)
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({"status": "ignored", "event_type": x_github_event})

        # Make engagement decision
        decision = await decide_engagement(event, repo, self._store)

        logger.info(
            "Webhook decision for %s: engage=%s, kind=%s, reason=%s",
            event.event_type,
            decision.should_engage,
            decision.activity_kind,
            decision.reason,
        )

        if not decision.should_engage:
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({
                "status": "no_action",
                "reason": decision.reason,
            })

        # Handle approval workflow
        if decision.activity_kind == "approve_activity":
            await self._handle_approval(event, payload)
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({
                "status": "approved",
                "event_id": x_github_delivery,
            })

        # Create activity
        activity_id = self._store.add_activity(
            repo_id=repo_id,
            kind=decision.activity_kind,
            trigger=event.trigger,
        )

        # Set approval requirement if needed
        if decision.requires_approval:
            self._store.update_activity(
                activity_id,
                requires_approval=1,
            )

        # Auto-index issues and record PR reviews for patrol dedup
        asyncio.ensure_future(
            self._handle_patrol_side_effects(x_github_event, payload, repo_id)
        )

        self._store.mark_webhook_event_processed(x_github_delivery)

        logger.info(
            "Created %s activity %s from webhook %s",
            decision.activity_kind,
            activity_id[:8],
            x_github_delivery,
        )

        return JSONResponse({
            "status": "created",
            "activity_id": activity_id,
            "activity_kind": decision.activity_kind,
            "event_id": x_github_delivery,
        })

    async def _handle_approval(self, event: Any, payload: dict[str, Any]) -> None:
        """Handle approval comment by transitioning pending_approval activity to pending."""
        from ..decision import check_user_is_admin

        trigger_parts = event.trigger.split(":")
        if len(trigger_parts) < 2:
            logger.warning("Invalid trigger format for approval: %s", event.trigger)
            return

        issue_or_pr = f"{trigger_parts[0]}:{trigger_parts[1]}"

        pending_approval = self._store.get_pending_approval_activities()
        matching_activity = None
        for activity in pending_approval:
            if activity["trigger"] == issue_or_pr:
                matching_activity = activity
                break

        if matching_activity is None:
            logger.warning("No pending approval activity found for: %s", issue_or_pr)
            return

        comment_author = event.actor
        repo = self._store.get_repo(event.repo_id)
        if repo is None:
            logger.error("Repository not found: %s", event.repo_id)
            return

        try:
            owner, repo_name = parse_repo_url(repo["repo_url"])
        except ValueError:
            logger.error("Invalid repo URL: %s", repo["repo_url"])
            return

        github_token = await self._auth.get_token()
        is_admin = await check_user_is_admin(comment_author, owner, repo_name, github_token)

        if not is_admin:
            logger.warning("User %s attempted to approve but lacks permissions", comment_author)
            return

        comment_url = payload.get("comment", {}).get("html_url", "")
        self._store.update_activity(
            matching_activity["id"],
            status="pending",
            requires_approval=0,
            approved_by=comment_author,
            approval_comment_url=comment_url,
        )
        logger.info("Activity %s approved by %s", matching_activity["id"][:8], comment_author)

    async def _handle_app_webhook(
        self,
        request: Request,
        x_github_event: str = Header(...),
        x_github_delivery: str = Header(...),
        x_hub_signature_256: str | None = Header(None),
    ) -> Response:
        """Handle GitHub App-level webhook (installation events + all repo events).

        GitHub sends all App events to this single endpoint with the App's
        webhook secret, rather than per-repo secrets.
        """
        app_secret = get_github_app_webhook_secret()
        body = await request.body()

        # Verify App-level signature
        if app_secret:
            if not x_hub_signature_256:
                raise HTTPException(status_code=401, detail="Missing signature")
            if not verify_signature(body, x_hub_signature_256, app_secret):
                raise HTTPException(status_code=401, detail="Invalid signature")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON")

        # Deduplicate
        if self._store.is_webhook_event_processed(x_github_delivery):
            return JSONResponse({"status": "duplicate", "event_id": x_github_delivery})

        # Store raw event for deduplication tracking
        self._store.add_webhook_event(
            event_id=x_github_delivery,
            repo_id="__app__",
            event_type=x_github_event,
            payload=json.dumps(payload),
        )

        logger.info("GitHub App webhook: %s (%s)", x_github_event, x_github_delivery)

        # Handle installation lifecycle events
        if x_github_event == "installation":
            result = await self._handle_installation_event(payload, x_github_delivery)
            return JSONResponse(result)

        if x_github_event == "installation_repositories":
            result = await self._handle_installation_repositories_event(payload, x_github_delivery)
            return JSONResponse(result)

        # Route regular repo events through the normal webhook pipeline
        # Identify the repo from the payload
        repo_info = payload.get("repository")
        if not repo_info:
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({"status": "ignored", "reason": "no repository in payload"})

        repo_url = repo_info.get("html_url", "")
        try:
            repo_id = repo_id_from_url(repo_url)
        except ValueError:
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({"status": "ignored", "reason": "invalid repo url"})

        repo = self._store.get_repo(repo_id)
        if repo is None or not repo["watch"]:
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({"status": "ignored", "reason": "repo not watched"})

        # Store and parse
        self._store.add_webhook_event(
            event_id=x_github_delivery,
            repo_id=repo_id,
            event_type=x_github_event,
            payload=json.dumps(payload),
        )
        event = parse_webhook(x_github_event, payload, x_github_delivery, repo_id)
        if event is None:
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({"status": "ignored", "event_type": x_github_event})

        decision = await decide_engagement(event, repo, self._store)
        if not decision.should_engage:
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({"status": "no_action", "reason": decision.reason})

        if decision.activity_kind == "approve_activity":
            await self._handle_approval(event, payload)
            self._store.mark_webhook_event_processed(x_github_delivery)
            return JSONResponse({"status": "approved"})

        activity_id = self._store.add_activity(repo_id, decision.activity_kind, event.trigger)
        if decision.requires_approval:
            self._store.update_activity(activity_id, requires_approval=1)

        # Auto-index issues and record PR reviews for patrol dedup
        asyncio.ensure_future(
            self._handle_patrol_side_effects(x_github_event, payload, repo_id)
        )

        self._store.mark_webhook_event_processed(x_github_delivery)
        return JSONResponse({"status": "created", "activity_id": activity_id, "activity_kind": decision.activity_kind})

    async def _handle_patrol_side_effects(
        self, event_type: str, payload: dict[str, Any], repo_id: str
    ) -> None:
        """Handle side effects for patrol: index issues and record PR file reviews."""
        try:
            repo = self._store.get_repo(repo_id)
            if repo is None:
                return

            from ..config import parse_repo_url
            try:
                owner, repo_name = parse_repo_url(repo["repo_url"])
            except ValueError:
                return

            github_token = await self._auth.get_token()
            if not github_token:
                return

            if event_type == "issues":
                issue = payload.get("issue", {})
                action = payload.get("action", "")
                issue_number = issue.get("number")
                if issue_number is None:
                    return

                if action in ("opened", "edited"):
                    from ..issue_indexer import index_single_issue
                    await index_single_issue(
                        repo_id, issue_number, owner, repo_name, github_token, self._store
                    )
                    logger.debug("Auto-indexed issue #%d for %s", issue_number, repo_id)
                elif action == "closed":
                    self._store.update_issue_status(repo_id, issue_number, "closed")
                    logger.debug("Marked issue #%d closed for %s", issue_number, repo_id)

            elif event_type == "pull_request":
                pr = payload.get("pull_request", {})
                action = payload.get("action", "")
                if action == "closed" and pr.get("merged"):
                    pr_number = pr.get("number")
                    merge_sha = pr.get("merge_commit_sha", "")
                    if pr_number and merge_sha:
                        pr_files = await self._get_pr_files(
                            owner, repo_name, pr_number, github_token
                        )
                        for file_path in pr_files:
                            self._store.upsert_reviewed_file(
                                repo_id, file_path, merge_sha, "pr_review"
                            )
                        logger.debug(
                            "Recorded %d PR #%d files as reviewed for %s",
                            len(pr_files), pr_number, repo_id,
                        )
        except Exception as e:
            logger.warning("Patrol side effects error for %s: %s", repo_id, e)

    async def _get_pr_files(
        self, owner: str, repo: str, pr_number: int, github_token: str
    ) -> list[str]:
        """Fetch list of files changed in a PR."""
        import httpx
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        file_paths: list[str] = []
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files",
                params={"per_page": 100},
                headers=headers,
                timeout=10.0,
            )
            if resp.status_code == 200:
                for f in resp.json():
                    fp = f.get("filename")
                    if fp:
                        file_paths.append(fp)
        return file_paths

    async def _index_repo_issues_background(self, repo_id: str, owner: str, repo_name: str) -> None:
        """Background task: index all open issues for a newly-added repo."""
        try:
            github_token = await self._auth.get_token()
            if not github_token:
                return
            from ..issue_indexer import index_repo_issues
            count = await index_repo_issues(repo_id, owner, repo_name, github_token, self._store)
            logger.info("Indexed %d issues for newly added repo %s", count, repo_id)
        except Exception as e:
            logger.warning("Background issue indexing failed for %s: %s", repo_id, e)

    async def _handle_installation_event(self, payload: dict, delivery_id: str) -> dict:
        """Handle GitHub App installation created/deleted events."""
        action = payload.get("action")
        installation = payload.get("installation", {})
        installation_id = str(installation.get("id", ""))
        account = installation.get("account", {})
        account_login = account.get("login", "")
        account_type = account.get("type", "User")
        repos = payload.get("repositories", [])

        if action == "created":
            self._store.add_installation(installation_id, account_login, account_type)
            watched = []
            for repo_info in repos:
                repo_url = f"https://github.com/{repo_info['full_name']}"
                repo_id = repo_id_from_url(repo_url)
                owner, repo_name = repo_info["full_name"].split("/", 1)
                self._store.add_repo(repo_id, repo_url)
                self._store.update_repo(repo_id, watch=1)
                # Link repo to user if installation is associated with one
                user_id = self._store.get_user_id_for_installation(installation_id)
                if user_id:
                    self._store.update_repo(repo_id, user_id=user_id)
                watched.append(repo_id)
                logger.info("Auto-watched repo from App installation: %s", repo_id)
                # Kick off background issue indexing for dedup
                asyncio.ensure_future(
                    self._index_repo_issues_background(repo_id, owner, repo_name)
                )
            self._store.mark_webhook_event_processed(delivery_id)
            return {"status": "installation_created", "watched_repos": watched}

        elif action == "deleted":
            watched_repos = [
                r["id"] for r in self._store.list_watched_repos()
                if r["repo_url"].startswith(f"https://github.com/{account_login}/")
            ]
            for repo_id in watched_repos:
                self._store.update_repo(repo_id, watch=0)
            self._store.delete_installation(installation_id)
            self._store.mark_webhook_event_processed(delivery_id)
            logger.info("App uninstalled by %s, unwatched %d repos", account_login, len(watched_repos))
            return {"status": "installation_deleted", "unwatched_repos": watched_repos}

        self._store.mark_webhook_event_processed(delivery_id)
        return {"status": "ignored", "action": action}

    async def _handle_installation_repositories_event(self, payload: dict, delivery_id: str) -> dict:
        """Handle repos being added/removed from an existing GitHub App installation."""
        action = payload.get("action")  # noqa: F841
        added = payload.get("repositories_added", [])
        removed = payload.get("repositories_removed", [])

        # Ensure installation record exists (handles case where 'created' event was missed)
        installation = payload.get("installation", {})
        installation_id = str(installation.get("id", ""))
        account = installation.get("account", {})
        account_login = account.get("login", "")
        account_type = account.get("type", "User")
        if installation_id:
            existing = self._store.get_installation(installation_id)
            if not existing:
                self._store.add_installation(installation_id, account_login, account_type)
                logger.info("Auto-created missing installation record: %s (%s)", installation_id, account_login)

        user_id = self._store.get_user_id_for_installation(installation_id) if installation_id else None

        watched, unwatched = [], []

        for repo_info in added:
            repo_url = f"https://github.com/{repo_info['full_name']}"
            repo_id = repo_id_from_url(repo_url)
            owner, repo_name = repo_info["full_name"].split("/", 1)
            self._store.add_repo(repo_id, repo_url)
            self._store.update_repo(repo_id, watch=1)
            if user_id:
                self._store.update_repo(repo_id, user_id=user_id)
            watched.append(repo_id)
            logger.info("Auto-watched repo added to installation: %s", repo_id)
            asyncio.ensure_future(
                self._index_repo_issues_background(repo_id, owner, repo_name)
            )

        for repo_info in removed:
            repo_url = f"https://github.com/{repo_info['full_name']}"
            repo_id = repo_id_from_url(repo_url)
            self._store.update_repo(repo_id, watch=0)
            unwatched.append(repo_id)
            logger.info("Auto-unwatched repo removed from installation: %s", repo_id)

        self._store.mark_webhook_event_processed(delivery_id)
        return {"status": "repositories_updated", "watched": watched, "unwatched": unwatched}

