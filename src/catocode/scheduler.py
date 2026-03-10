from __future__ import annotations

"""Scheduler — the daemon's main loop.

Three concurrent loops:
1. _approval_loop: Every 30s, check for approval comments on pending_approval activities
2. _patrol_loop: Per-repo interval, trigger proactive code audits
3. _dispatch_loop: Every 5s, pick up pending activities and dispatch

Concurrency model:
- Per-repo asyncio.Lock: activities on the same repo run serially
- Global asyncio.Semaphore: caps total concurrent activities
"""

import asyncio
import logging
import signal
from datetime import datetime, timezone

from .auth import Auth, get_auth
from .config import get_anthropic_api_key, get_anthropic_base_url, parse_repo_url
from .container.manager import ContainerManager
from .container.registry import ContainerRegistry
from .decision import check_user_is_admin
from .dispatcher import dispatch
from .store import Store

logger = logging.getLogger(__name__)

APPROVAL_CHECK_INTERVAL_SECS = 30
DISPATCH_CHECK_INTERVAL_SECS = 5
MAX_CONCURRENT = 3  # Global max concurrent activities


class Scheduler:
    def __init__(
        self,
        store: Store,
        container_mgr: ContainerManager | None = None,
        max_concurrent: int = MAX_CONCURRENT,
        verbose: bool = False,
        auth: Auth | None = None,
    ) -> None:
        self._store = store
        # Support both legacy single-manager (CLI) and per-user registry (SaaS)
        if container_mgr is not None:
            self._container_mgr = container_mgr
            self._registry: ContainerRegistry | None = None
        else:
            self._container_mgr = None
            self._registry = ContainerRegistry()
        self._max_concurrent = max_concurrent
        self._verbose = verbose
        self._auth = auth or get_auth()

        # Per-repo serial lock
        self._repo_locks: dict[str, asyncio.Lock] = {}
        # Global concurrency cap
        self._semaphore = asyncio.Semaphore(max_concurrent)
        # Shutdown event
        self._stop_event = asyncio.Event()

    def _repo_lock(self, repo_id: str) -> asyncio.Lock:
        if repo_id not in self._repo_locks:
            self._repo_locks[repo_id] = asyncio.Lock()
        return self._repo_locks[repo_id]

    async def run(self) -> None:
        """Start all loops. Runs until SIGTERM/SIGINT or stop() called."""
        # Mark crashed activities from previous run
        crashed = self._store.mark_crashed_activities_failed()
        if crashed > 0:
            logger.warning("Marked %d crashed activities as failed from previous run", crashed)

        # Install signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self._stop_event.set())

        logger.info("Scheduler started (max_concurrent=%d)", self._max_concurrent)

        try:
            await asyncio.gather(
                self._approval_loop(),
                self._patrol_loop(),
                self._dispatch_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            logger.info("Scheduler stopped")

    def stop(self) -> None:
        """Signal the scheduler to stop gracefully."""
        self._stop_event.set()

    async def _approval_loop(self) -> None:
        """Check for approval comments on pending_approval activities every 30s."""
        while not self._stop_event.is_set():
            try:
                pending_approval = self._store.get_pending_approval_activities()
                for activity in pending_approval:
                    try:
                        await self._check_for_approval(activity)
                    except Exception as e:
                        logger.error(
                            "Approval check error for activity %s: %s",
                            activity["id"][:8],
                            e,
                        )
            except Exception as e:
                logger.error("Approval loop error: %s", e)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=APPROVAL_CHECK_INTERVAL_SECS,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _check_for_approval(self, activity: dict) -> None:
        """Check if admin posted approval comment for this activity.

        Args:
            activity: Activity record waiting for approval
        """
        import httpx

        repo = self._store.get_repo(activity["repo_id"])
        if repo is None:
            logger.error("Repository not found: %s", activity["repo_id"])
            return

        try:
            owner, repo_name = parse_repo_url(repo["repo_url"])
        except ValueError:
            logger.error("Invalid repo URL: %s", repo["repo_url"])
            return

        # Extract issue/PR number from trigger
        trigger = activity["trigger"]
        if not trigger:
            return

        parts = trigger.split(":")
        if len(parts) < 2:
            return

        issue_or_pr_type = parts[0]  # "issue" or "pr"  # noqa: F841
        number = parts[1]

        # Fetch recent comments
        github_token = await self._auth.get_token()
        if not github_token:
            logger.error("GitHub token not configured")
            return

        url = f"https://api.github.com/repos/{owner}/{repo_name}/issues/{number}/comments"
        headers = {
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, headers=headers, timeout=10.0)
                if response.status_code != 200:
                    logger.error("Failed to fetch comments: %s", response.status_code)
                    return

                comments = response.json()

                # Check recent comments for approval keywords
                approval_keywords = ["/approve", "/fix", "go ahead", "@catocode fix"]

                for comment in reversed(comments[-10:]):  # Check last 10 comments
                    comment_body = comment.get("body", "").lower()
                    comment_author = comment.get("user", {}).get("login", "")
                    comment_url = comment.get("html_url", "")

                    # Check if comment contains approval keyword
                    if any(keyword in comment_body for keyword in approval_keywords):
                        # Verify user is admin
                        is_admin = await check_user_is_admin(
                            comment_author, owner, repo_name, github_token
                        )

                        if is_admin:
                            # Approve the activity
                            self._store.update_activity(
                                activity["id"],
                                status="pending",
                                requires_approval=0,
                                approved_by=comment_author,
                                approval_comment_url=comment_url,
                            )
                            logger.info(
                                "Activity %s approved by %s",
                                activity["id"][:8],
                                comment_author,
                            )
                            return

        except Exception as e:
            logger.error("Failed to check for approval: %s", e)

    async def _patrol_loop(self) -> None:
        """Schedule patrol activities for repos that haven't been audited recently."""
        while not self._stop_event.is_set():
            try:
                repos = self._store.list_watched_repos()
                for repo in repos:
                    try:
                        await self._maybe_schedule_patrol(repo)
                    except Exception as e:
                        logger.error("Patrol scheduling error for %s: %s", repo["id"], e)
            except Exception as e:
                logger.error("Patrol loop error: %s", e)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=3600,  # Check every hour
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _maybe_schedule_patrol(self, repo: dict) -> None:
        """Schedule a patrol activity if interval has passed and budget allows."""
        repo_id = repo["id"]

        # Skip if patrol not enabled (default: disabled)
        if not repo.get("patrol_enabled"):
            logger.debug("Patrol disabled for %s, skipping", repo_id)
            return

        interval_hours = repo.get("patrol_interval_hours") or 12

        # Check if enough time has passed since last patrol
        activities = self._store.list_activities(repo_id)
        last_patrol = None
        for a in reversed(list(activities)):
            if a["kind"] == "patrol" and a["status"] in ("done", "running"):
                last_patrol = datetime.fromisoformat(a["updated_at"])
                break

        if last_patrol is not None:
            hours_since = (datetime.now(timezone.utc) - last_patrol).total_seconds() / 3600
            if hours_since < interval_hours:
                return

        # Check patrol budget
        budget = self._store.get_patrol_budget(repo_id)
        if budget <= 0:
            logger.debug("Patrol budget exhausted for %s, skipping", repo_id)
            return

        # Resolve container manager for git commands
        container_mgr = self._container_mgr
        if self._registry is not None:
            user_id = repo.get("user_id")
            if user_id:
                container_mgr = self._registry.get(user_id)
            else:
                container_mgr = ContainerManager()

        # Try to compute changed files via git diff in container
        last_sha = repo.get("last_patrol_sha")
        current_sha: str | None = None
        changed_files: list[str] | None = None

        if container_mgr is not None:
            repo_path = f"/repos/{repo_id}"
            try:
                result = container_mgr.exec(f"git -C {repo_path} rev-parse HEAD 2>/dev/null")
                if result.exit_code == 0:
                    current_sha = result.stdout.strip()

                    if last_sha and last_sha != current_sha:
                        diff_result = container_mgr.exec(
                            f"git -C {repo_path} diff --name-only {last_sha}..{current_sha} 2>/dev/null"
                        )
                        if diff_result.exit_code == 0:
                            changed_files = [
                                f for f in diff_result.stdout.strip().splitlines() if f
                            ]
                        logger.debug(
                            "Git diff %s..%s: %d files changed",
                            last_sha[:8] if last_sha else "?",
                            current_sha[:8] if current_sha else "?",
                            len(changed_files) if changed_files else 0,
                        )
            except Exception as e:
                logger.debug("Could not get git diff for %s (container not ready?): %s", repo_id, e)
                # Fall through: changed_files remains None → full scan

        # Filter: exclude files already reviewed at this exact SHA
        if changed_files is not None and current_sha:
            reviewed = self._store.get_reviewed_files(repo_id)
            reviewed_unchanged = {
                r["file_path"] for r in reviewed if r["commit_sha"] == current_sha
            }
            # Exclude files with existing CatoCode open issues
            catocode_issue_files = self._store.get_catocode_open_issue_files(repo_id)

            filtered = [
                f for f in changed_files
                if f not in reviewed_unchanged and f not in catocode_issue_files
            ]

            if not filtered:
                logger.info(
                    "No files to patrol for %s after filtering, updating SHA", repo_id
                )
                if current_sha:
                    self._store.update_last_patrol_sha(repo_id, current_sha)
                return

            changed_files = filtered

        # Build trigger: budget:N|sha:SHA
        trigger_parts = [f"budget:{budget}"]
        if current_sha:
            trigger_parts.append(f"sha:{current_sha}")
        trigger = "|".join(trigger_parts)

        # Store changed_files in activity metadata
        metadata = {"changed_files": changed_files} if changed_files is not None else {}

        activity_id = self._store.add_activity(
            repo_id, "patrol", trigger, metadata=metadata
        )
        logger.info(
            "Scheduled patrol activity %s for %s (budget=%d, files=%s)",
            activity_id[:8],
            repo_id,
            budget,
            len(changed_files) if changed_files is not None else "all",
        )

    async def _dispatch_loop(self) -> None:
        """Pick up pending activities and dispatch them."""
        while not self._stop_event.is_set():
            try:
                pending = self._store.get_pending_activities()
                for activity in pending:
                    # Don't await — fire and forget (respects semaphore)
                    asyncio.ensure_future(
                        self._dispatch_one(activity["id"], activity["repo_id"])
                    )
            except Exception as e:
                logger.error("Dispatch loop error: %s", e)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=DISPATCH_CHECK_INTERVAL_SECS,
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _dispatch_one(self, activity_id: str, repo_id: str) -> None:
        """Dispatch one activity with per-repo serial lock and global semaphore."""
        async with self._semaphore:
            async with self._repo_lock(repo_id):
                # Re-check status — another coroutine might have grabbed it
                activity = self._store.get_activity(activity_id)
                if activity is None or activity["status"] != "pending":
                    return

                # Resolve container manager: per-user registry or legacy single manager
                if self._registry is not None:
                    repo = self._store.get_repo(repo_id)
                    user_id = repo.get("user_id") if repo else None
                    if user_id:
                        container_mgr = self._registry.get(user_id)
                    else:
                        # Fallback: legacy container for CLI-mode repos without user_id
                        container_mgr = ContainerManager()
                else:
                    container_mgr = self._container_mgr

                try:
                    await dispatch(
                        activity_id=activity_id,
                        store=self._store,
                        container_mgr=container_mgr,
                        anthropic_api_key=get_anthropic_api_key(),
                        github_token=await self._auth.get_token(),
                        anthropic_base_url=get_anthropic_base_url(),
                        verbose=self._verbose,
                    )
                except Exception as e:
                    logger.error("Dispatch failed for activity %s: %s", activity_id[:8], e)
