from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from .config import parse_repo_url
from .github.commenter import failure_comment, post_issue_comment
from .github.issue_fetcher import fetch_issue
from .skill_renderer import (
    build_analyze_issue_prompt,
    build_fix_issue_prompt,
    build_patrol_prompt,
    build_respond_review_prompt,
    build_review_pr_prompt,
    build_triage_prompt,
)
from .templates.init_prompt import get_init_prompt

if TYPE_CHECKING:
    from .container.manager import ContainerManager
    from .store import Store

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECS = 600   # 10 minutes without output → kill
HARD_TIMEOUT_SECS = 7200  # 2 hours absolute maximum
MAX_RETRIES = 3           # SDK runner retries on transient failure
RETRY_DELAY_SECS = 30     # Delay between retries


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
        prompt = await _build_prompt(activity, repo, github_token, store)

        # 6. Update status to running
        store.update_activity(activity_id, status="running")

        # 7. Determine session resume for respond_review
        resume_session_id = None
        if activity["kind"] == "respond_review":
            resume_session_id = _find_original_session_id(activity, store)

        # 8. Execute SDK runner with retries on transient failure
        exit_code = 1
        session_id = None
        cost_usd = None
        for attempt in range(1, MAX_RETRIES + 1):
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
            if exit_code == 0:
                break
            if attempt < MAX_RETRIES:
                logger.warning(
                    "Activity %s attempt %d/%d failed, retrying in %ds",
                    activity_id[:8], attempt, MAX_RETRIES, RETRY_DELAY_SECS,
                )
                await asyncio.sleep(RETRY_DELAY_SECS)
                # Reset repo to clean state before retry
                if activity["kind"] != "respond_review":
                    container_mgr.reset_repo(repo_id)
            else:
                logger.error(
                    "Activity %s failed after %d attempts", activity_id[:8], MAX_RETRIES
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
                cost_usd=cost_usd,
            )
            logger.info("Activity %s completed (cost=$%.4f)", activity_id, cost_usd or 0)
        else:
            store.update_activity(
                activity_id,
                status="failed",
                summary=summary,
                session_id=session_id,
                cost_usd=cost_usd,
            )
            logger.warning("Activity %s failed after %d attempts", activity_id[:8], MAX_RETRIES)
            await _notify_failure(activity, repo, github_token, summary)

    except asyncio.TimeoutError:
        summary = "Timeout: activity exceeded time limit"
        store.update_activity(activity_id, status="failed", summary=summary)
        logger.error("Activity %s timed out", activity_id)
        await _notify_failure(activity, repo, github_token, summary)
        raise
    except Exception as e:
        summary = f"Error: {e}"
        store.update_activity(activity_id, status="failed", summary=summary)
        logger.exception("Activity %s failed with exception", activity_id)
        await _notify_failure(activity, repo, github_token, summary)
        raise


async def _notify_failure(
    activity: dict,
    repo: dict | None,
    github_token: str,
    error_summary: str,
) -> None:
    """Post a failure comment on the relevant PR/issue if applicable."""
    if not github_token or repo is None:
        return
    trigger = activity.get("trigger") or ""
    kind = activity.get("kind", "")

    # Determine target: PR or issue number from trigger
    issue_number: int | None = None
    if trigger.startswith("pr:"):
        try:
            issue_number = int(trigger.split(":")[1])
        except (IndexError, ValueError):
            pass
    elif trigger.startswith("issue:"):
        try:
            issue_number = int(trigger.split(":")[1])
        except (IndexError, ValueError):
            pass

    if issue_number is None:
        return

    try:
        owner, repo_name = parse_repo_url(repo["repo_url"])
    except ValueError:
        return

    body = failure_comment(kind, error_summary)
    await post_issue_comment(owner, repo_name, issue_number, body, github_token)


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


async def _build_prompt(activity: dict, repo: dict, github_token: str, store: "Store | None" = None) -> str:
    """Build prompt based on activity kind using skill-based templates."""
    kind = activity["kind"]
    trigger = activity["trigger"] or ""
    owner, repo_name = parse_repo_url(repo["repo_url"])

    if kind == "init":
        return get_init_prompt()

    elif kind == "fix_issue":
        # Trigger format: "issue:123"
        if not trigger.startswith("issue:"):
            raise ValueError(f"Invalid trigger for fix_issue: {trigger!r}")
        issue_number = trigger.split(":", 1)[1]
        issue = await fetch_issue(owner, repo_name, int(issue_number), github_token)

        # Format issue data for the skill
        issue_data = f"""Title: {issue.title}
Author: {issue.author}
Created: {issue.created_at}

{issue.body}
"""
        return build_fix_issue_prompt(
            issue_number=issue_number,
            repo_id=repo.get("id", f"{owner}-{repo_name}"),
            issue_data=issue_data,
        )

    elif kind == "triage":
        # Trigger format: "issue:123"
        if not trigger.startswith("issue:"):
            raise ValueError(f"Invalid trigger for triage: {trigger!r}")
        issue_number = trigger.split(":", 1)[1]
        issue = await fetch_issue(owner, repo_name, int(issue_number), github_token)

        issue_data = f"""Title: {issue.title}
Author: {issue.author}
Created: {issue.created_at}

{issue.body}
"""
        return build_triage_prompt(
            issue_number=issue_number,
            repo_id=repo.get("id", f"{owner}-{repo_name}"),
            issue_data=issue_data,
        )

    elif kind == "analyze_issue":
        # Trigger format: "issue:123"
        if not trigger.startswith("issue:"):
            raise ValueError(f"Invalid trigger for analyze_issue: {trigger!r}")
        issue_number = trigger.split(":", 1)[1]
        issue = await fetch_issue(owner, repo_name, int(issue_number), github_token)

        issue_data = f"""Title: {issue.title}
Author: {issue.author}
Created: {issue.created_at}
Labels: {', '.join(issue.labels) if issue.labels else 'None'}

{issue.body}
"""
        # RAG: find potential duplicates
        from .embeddings import is_embedding_service_configured
        from .issue_indexer import find_duplicates
        relevant_issues: list[dict] = []
        if store is not None and is_embedding_service_configured():
            try:
                query = f"{issue.title}\n{issue.body[:500]}"
                relevant_issues = await find_duplicates(repo["id"], query, store)
            except Exception:
                pass

        return build_analyze_issue_prompt(
            issue_number=issue_number,
            repo_id=repo.get("id", f"{owner}-{repo_name}"),
            issue_data=issue_data,
            relevant_issues=relevant_issues,
        )

    elif kind == "patrol":
        # Trigger format: "budget:N" or "budget:N|sha:SHA"
        import json as _json
        budget = 5  # default
        current_sha: str | None = None

        for part in trigger.split("|"):
            if part.startswith("budget:"):
                try:
                    budget = int(part.split(":", 1)[1])
                except ValueError:
                    pass
            elif part.startswith("sha:"):
                current_sha = part.split(":", 1)[1]

        # Retrieve changed_files from activity metadata
        changed_files: list[str] | None = None
        raw_metadata = activity.get("metadata")
        if raw_metadata:
            try:
                meta = _json.loads(raw_metadata)
                changed_files = meta.get("changed_files")
            except Exception:
                pass

        # RAG: query relevant issues if embedding service configured
        from .embeddings import is_embedding_service_configured
        from .issue_indexer import find_duplicates

        relevant_issues: list[dict] = []
        if store is not None and is_embedding_service_configured() and changed_files:
            # Use changed file list as query for relevant issues
            query = "issues related to: " + ", ".join(changed_files[:20])
            try:
                relevant_issues = await find_duplicates(repo["id"], query, store)
            except Exception:
                pass  # Graceful degradation

        return build_patrol_prompt(
            repo_id=repo.get("id", f"{owner}-{repo_name}"),
            budget_remaining=budget,
            changed_files=changed_files,
            relevant_issues=relevant_issues,
            current_sha=current_sha,
        )

    elif kind == "task":
        # Trigger format: "pr:123:instruction" or "issue:123:instruction" or plain instruction
        if trigger and trigger.startswith("pr:"):
            parts = trigger.split(":", 2)
            pr_num = parts[1]
            instruction = parts[2] if len(parts) > 2 else "See PR for context."
            return (
                f"You were mentioned in a comment on PR #{pr_num} of "
                f"https://github.com/{owner}/{repo_name}/pull/{pr_num}.\n\n"
                f"The request was: {instruction}\n\n"
                f"Read the PR (use `gh pr view {pr_num} --comments`) to understand the context, "
                f"then carry out the requested task.\n\n"
                f"When done, reply to the PR with a summary of what you did: "
                f"`gh pr comment {pr_num} --body \"...\"`"
            )
        elif trigger and trigger.startswith("issue:"):
            parts = trigger.split(":", 2)
            issue_num = parts[1]
            instruction = parts[2] if len(parts) > 2 else "See issue for context."
            return (
                f"You were mentioned in a comment on issue #{issue_num} of "
                f"https://github.com/{owner}/{repo_name}/issues/{issue_num}.\n\n"
                f"The request was: {instruction}\n\n"
                f"Read the issue (use `gh issue view {issue_num} --comments`) to understand the context, "
                f"then carry out the requested task.\n\n"
                f"When done, reply to the issue with a summary of what you did: "
                f"`gh issue comment {issue_num} --body \"...\"`"
            )
        return trigger or "Execute the task as described."

    elif kind == "respond_review":
        # Trigger format: "pr:123"
        if not trigger.startswith("pr:"):
            raise ValueError(f"Invalid trigger for respond_review: {trigger!r}")
        pr_number = trigger.split(":", 1)[1]

        # Fetch review comments (placeholder - actual implementation would use gh pr view)
        review_comments = f"(Read from the PR itself using `gh pr view {pr_number} --comments`)"

        return build_respond_review_prompt(
            pr_number=pr_number,
            repo_id=repo.get("id", f"{owner}-{repo_name}"),
            review_comments=review_comments,
        )

    elif kind == "review_pr":
        # Trigger format: "pr:123"
        if not trigger.startswith("pr:"):
            raise ValueError(f"Invalid trigger for review_pr: {trigger!r}")
        pr_number = trigger.split(":", 1)[1]

        # Fetch PR details using gh CLI
        pr_data = f"(Use `gh pr view {pr_number}` and `gh pr diff {pr_number}` to read the PR)"

        return build_review_pr_prompt(
            pr_number=pr_number,
            repo_id=repo.get("id", f"{owner}-{repo_name}"),
            pr_data=pr_data,
        )

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
