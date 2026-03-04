from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from .config import parse_repo_url, repo_id_from_url
from .github.issue_fetcher import fetch_issue
from .templates.init_prompt import get_init_prompt
from .templates.prompts import (
    fix_issue_prompt,
    patrol_prompt,
    respond_review_prompt,
    review_pr_prompt,
    triage_prompt,
)

if TYPE_CHECKING:
    from .container.manager import ContainerManager
    from .store import Store

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECS = 600   # 10 minutes without output → kill
HARD_TIMEOUT_SECS = 7200  # 2 hours absolute maximum


async def dispatch(
    activity_id: str,
    store: Store,
    container_mgr: ContainerManager,
    anthropic_api_key: str,
    github_token: str,
    anthropic_base_url: str | None = None,
    max_turns: int = 200,
    verbose: bool = False,
) -> None:
    """Dispatch a single activity: ensure container/repo ready, run SDK runner, stream logs to DB.

    Updates activity status: pending → running → done|failed
    """
    activity = store.get_activity(activity_id)
    if activity is None:
        raise ValueError(f"Activity {activity_id} not found")

    repo = store.get_repo(activity["repo_id"])
    if repo is None:
        raise ValueError(f"Repo {activity['repo_id']} not found")

    repo_id = repo["id"]
    repo_url = repo["repo_url"]

    logger.info("Dispatching activity %s (kind=%s, repo=%s)", activity_id, activity["kind"], repo_id)

    try:
        # 1. Ensure container running
        container_mgr.ensure_running(anthropic_api_key, github_token, anthropic_base_url)

        # 2. Ensure repo cloned
        container_mgr.ensure_repo(repo_id, repo_url)

        # 3. Check if repo needs init (look at origin, since local is about to be reset)
        result = container_mgr.exec(
            "git ls-tree -r HEAD --name-only | grep -x CLAUDE.md",
            workdir=f"/repos/{repo_id}",
        )
        needs_init = result.exit_code != 0

        if needs_init:
            logger.info("Repo %s needs init, running init activity first", repo_id)
            init_activity_id = store.add_activity(repo_id, "init", "auto")
            await _run_init(init_activity_id, repo_id, store, container_mgr, verbose)

        # 4. Reset repo to clean state (skip for respond_review — needs existing PR branch)
        if activity["kind"] != "respond_review":
            container_mgr.reset_repo(repo_id)

        # 5. Build prompt based on activity kind
        prompt = await _build_prompt(activity, repo, github_token)

        # 6. Update status to running
        store.update_activity(activity_id, status="running")

        # 7. Determine session resume for respond_review
        resume_session_id = None
        if activity["kind"] == "respond_review":
            resume_session_id = _find_original_session_id(activity, store)

        # 8. Execute SDK runner and stream logs
        exit_code, session_id, cost_usd = await _execute_sdk_runner(
            activity_id=activity_id,
            repo_id=repo_id,
            prompt=prompt,
            store=store,
            container_mgr=container_mgr,
            max_turns=max_turns,
            session_id=resume_session_id,
            verbose=verbose,
        )

        # 9. Extract summary from result line
        summary = _extract_summary(store.get_logs(activity_id))

        # 10. Update final status and session_id for future resume
        if exit_code == 0:
            store.update_activity(
                activity_id,
                status="done",
                summary=summary,
                session_id=session_id,
            )
            logger.info("Activity %s completed (cost=$%.4f)", activity_id, cost_usd or 0)
        else:
            store.update_activity(
                activity_id,
                status="failed",
                summary=summary,
                session_id=session_id,
            )
            logger.warning("Activity %s failed with exit code %d", activity_id, exit_code)

    except asyncio.TimeoutError:
        store.update_activity(activity_id, status="failed", summary="Timeout")
        logger.error("Activity %s timed out", activity_id)
        raise
    except Exception as e:
        store.update_activity(activity_id, status="failed", summary=f"Error: {e}")
        logger.exception("Activity %s failed with exception", activity_id)
        raise


async def _run_init(
    activity_id: str,
    repo_id: str,
    store: Store,
    container_mgr: ContainerManager,
    verbose: bool,
) -> None:
    """Run init activity to explore repo and generate CLAUDE.md."""
    prompt = get_init_prompt()
    store.update_activity(activity_id, status="running")

    exit_code, session_id, _ = await _execute_sdk_runner(
        activity_id=activity_id,
        repo_id=repo_id,
        prompt=prompt,
        store=store,
        container_mgr=container_mgr,
        max_turns=50,  # Init uses more generous 50 turns for thorough exploration
        verbose=verbose,
    )

    summary = _extract_summary(store.get_logs(activity_id))
    if exit_code == 0:
        store.update_activity(activity_id, status="done", summary=summary, session_id=session_id)
        logger.info("Init activity %s completed", activity_id)
    else:
        store.update_activity(activity_id, status="failed", summary=summary)
        logger.warning("Init activity %s failed", activity_id)


async def _build_prompt(activity: dict, repo: dict, github_token: str) -> str:
    """Build prompt based on activity kind using typed prompt templates."""
    kind = activity["kind"]
    trigger = activity["trigger"] or ""
    owner, repo_name = parse_repo_url(repo["repo_url"])

    if kind == "init":
        return get_init_prompt()

    elif kind == "fix_issue":
        # Trigger format: "issue:123"
        if not trigger.startswith("issue:"):
            raise ValueError(f"Invalid trigger for fix_issue: {trigger!r}")
        issue_number = int(trigger.split(":", 1)[1])
        issue = await fetch_issue(owner, repo_name, issue_number, github_token)
        return fix_issue_prompt(
            issue_number=issue.number,
            issue_title=issue.title,
            issue_body=issue.body,
            repo_owner=owner,
            repo_name=repo_name,
        )

    elif kind == "triage":
        # Trigger format: "issue:123"
        if not trigger.startswith("issue:"):
            raise ValueError(f"Invalid trigger for triage: {trigger!r}")
        issue_number = int(trigger.split(":", 1)[1])
        issue = await fetch_issue(owner, repo_name, issue_number, github_token)
        return triage_prompt(
            issue_number=issue.number,
            issue_title=issue.title,
            issue_body=issue.body,
            issue_author=issue.author,
        )

    elif kind == "patrol":
        # Trigger format: "budget:N" or None
        budget = 5  # default
        if trigger.startswith("budget:"):
            budget = int(trigger.split(":", 1)[1])
        return patrol_prompt(repo_id=repo["id"], budget_remaining=budget)

    elif kind == "task":
        # Free-form instruction; trigger is the full instruction text
        return trigger or "Execute the task as described."

    elif kind == "respond_review":
        # Trigger format: "pr:123"
        if not trigger.startswith("pr:"):
            raise ValueError(f"Invalid trigger for respond_review: {trigger!r}")
        pr_number = int(trigger.split(":", 1)[1])
        return respond_review_prompt(pr_number=pr_number, review_comments="(Read from the PR itself using `gh pr view {pr_number} --comments`)")

    elif kind == "review_pr":
        # Trigger format: "pr:123"
        if not trigger.startswith("pr:"):
            raise ValueError(f"Invalid trigger for review_pr: {trigger!r}")
        pr_number = int(trigger.split(":", 1)[1])
        return review_pr_prompt(pr_number=pr_number, pr_title="(Read from the PR)", pr_diff="(Use `gh pr diff {pr_number}` to get the diff)")

    else:
        raise ValueError(f"Unknown activity kind: {kind!r}")


def _find_original_session_id(activity: dict, store: Store) -> str | None:
    """For respond_review, find the session_id from the original fix_issue activity."""
    trigger = activity.get("trigger") or ""
    if not trigger.startswith("pr:"):
        return None
    # Look for a fix_issue or review_pr activity on this repo that has a session_id
    activities = store.list_activities(repo_id=activity["repo_id"])
    for a in reversed(list(activities)):
        if a["kind"] in ("fix_issue", "review_pr") and a["session_id"]:
            logger.debug("Resuming session %s for respond_review", a["session_id"])
            return a["session_id"]
    return None


async def _execute_sdk_runner(
    activity_id: str,
    repo_id: str,
    prompt: str,
    store: Store,
    container_mgr: ContainerManager,
    max_turns: int,
    session_id: str | None = None,
    verbose: bool = False,
) -> tuple[int, str | None, float | None]:
    """Execute SDK runner and stream JSONL output to DB.

    Returns (exit_code, session_id, cost_usd).

    Two-layer timeout:
    - Idle timeout: IDLE_TIMEOUT_SECS seconds without any output
    - Hard timeout: HARD_TIMEOUT_SECS seconds absolute
    """
    cwd = f"/repos/{repo_id}"
    line_count = 0
    exit_code = 1
    result_session_id: str | None = None
    result_cost_usd: float | None = None

    log_batch: list[str] = []

    async def _flush_batch() -> None:
        nonlocal log_batch
        if log_batch:
            for line in log_batch:
                store.add_log(activity_id, line)
            log_batch = []

    async def _stream_with_idle_timeout() -> tuple[int, str | None, float | None]:
        nonlocal line_count, exit_code, result_session_id, result_cost_usd

        last_output_time = asyncio.get_event_loop().time()

        async for line, code in container_mgr.exec_sdk_runner(
            prompt=prompt,
            cwd=cwd,
            max_turns=max_turns,
            session_id=session_id,
        ):
            now = asyncio.get_event_loop().time()
            idle_secs = now - last_output_time

            if idle_secs > IDLE_TIMEOUT_SECS:
                logger.warning(
                    "Activity %s idle for %.0fs — killing", activity_id, idle_secs
                )
                raise asyncio.TimeoutError(f"Idle timeout after {idle_secs:.0f}s")

            if line is not None:
                last_output_time = now
                log_batch.append(line)
                line_count += 1

                # Flush every 50 lines
                if len(log_batch) >= 50:
                    await _flush_batch()

                # Extract session_id and cost from result line
                if line.strip().startswith("{"):
                    try:
                        obj = json.loads(line)
                        if obj.get("type") == "result":
                            result_session_id = obj.get("session_id")
                            result_cost_usd = obj.get("cost_usd")
                    except json.JSONDecodeError:
                        pass

                if verbose:
                    logger.debug("[%s] %s", activity_id[:8], line.rstrip())
            else:
                # Sentinel: exit code (code=0 is success, None means unknown → fail)
                exit_code = code if code is not None else 1

        await _flush_batch()
        logger.info(
            "SDK runner completed: exit=%d lines=%d session=%s cost=$%.4f",
            exit_code,
            line_count,
            result_session_id or "none",
            result_cost_usd or 0,
        )
        return exit_code, result_session_id, result_cost_usd

    # Wrap with hard timeout
    try:
        return await asyncio.wait_for(
            _stream_with_idle_timeout(),
            timeout=HARD_TIMEOUT_SECS,
        )
    except asyncio.TimeoutError:
        await _flush_batch()
        logger.error("Activity %s hit hard timeout (%ds)", activity_id, HARD_TIMEOUT_SECS)
        raise


def _extract_summary(logs: list) -> str:
    """Extract summary from result log line (max 500 chars)."""
    if not logs:
        return "No output"

    # Try to find result line in last 10 log entries
    for log in reversed(logs[-10:]):
        line = log["line"]
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                result_text = obj.get("result", "")
                if result_text:
                    return result_text[:500]
        except (json.JSONDecodeError, KeyError):
            continue

    # Fallback: last few lines as plain text
    last_lines = [log["line"] for log in logs[-5:]]
    return "\n".join(last_lines)[:500]


def _slugify(text: str) -> str:
    """Convert text to URL-safe slug, max 50 chars."""
    slug = text.lower()
    slug = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in slug)
    slug = "-".join(filter(None, slug.split("-")))
    return slug[:50]
