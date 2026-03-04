from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import TYPE_CHECKING

from .config import parse_repo_url, repo_id_from_url
from .github.issue_fetcher import fetch_issue
from .templates.init_prompt import get_init_prompt

if TYPE_CHECKING:
    from .container.manager import ContainerManager
    from .store import Store

logger = logging.getLogger(__name__)


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
    """
    Dispatch a single activity: ensure container/repo ready, run claude -p, stream logs to DB.

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

        # 3. Check if repo needs init
        result = container_mgr.exec(f"test -f /repos/{repo_id}/CLAUDE.md")
        needs_init = result.exit_code != 0

        if needs_init:
            logger.info("Repo %s needs init, running init activity first", repo_id)
            init_activity_id = store.add_activity(repo_id, "init", "auto")
            await _run_init(init_activity_id, repo_id, store, container_mgr, max_turns, verbose)

        # 4. Reset repo to clean state
        container_mgr.reset_repo(repo_id)

        # 5. Build prompt based on activity kind
        prompt = await _build_prompt(activity, repo, github_token)

        # 6. Update status to running
        store.update_activity(activity_id, status="running")

        # 7. Execute claude -p and stream logs
        exit_code = await _execute_claude(
            activity_id=activity_id,
            repo_id=repo_id,
            prompt=prompt,
            store=store,
            container_mgr=container_mgr,
            max_turns=max_turns,
            verbose=verbose,
        )

        # 8. Extract summary from last few log lines
        summary = _extract_summary(store.get_logs(activity_id))

        # 9. Update final status
        if exit_code == 0:
            store.update_activity(activity_id, status="done", summary=summary)
            logger.info("Activity %s completed successfully", activity_id)
        else:
            store.update_activity(activity_id, status="failed", summary=summary)
            logger.warning("Activity %s failed with exit code %d", activity_id, exit_code)

    except asyncio.TimeoutError:
        store.update_activity(activity_id, status="failed", summary="Timeout after 2 hours")
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
    max_turns: int,
    verbose: bool,
) -> None:
    """Run init activity to explore repo and generate CLAUDE.md."""
    prompt = get_init_prompt()
    store.update_activity(activity_id, status="running")

    exit_code = await _execute_claude(
        activity_id=activity_id,
        repo_id=repo_id,
        prompt=prompt,
        store=store,
        container_mgr=container_mgr,
        max_turns=30,  # Init uses fixed 30 turns
        verbose=verbose,
    )

    summary = _extract_summary(store.get_logs(activity_id))
    if exit_code == 0:
        store.update_activity(activity_id, status="done", summary=summary)
        logger.info("Init activity %s completed", activity_id)
    else:
        store.update_activity(activity_id, status="failed", summary=summary)
        logger.warning("Init activity %s failed", activity_id)


async def _build_prompt(activity: dict, repo: dict, github_token: str) -> str:
    """Build prompt based on activity kind."""
    kind = activity["kind"]
    trigger = activity["trigger"]

    if kind == "init":
        return get_init_prompt()

    elif kind == "fix_issue":
        # Parse issue number from trigger (format: "issue:123")
        if not trigger or not trigger.startswith("issue:"):
            raise ValueError(f"Invalid trigger for fix_issue: {trigger}")
        issue_number = int(trigger.split(":", 1)[1])

        # Fetch issue from GitHub
        owner, repo_name = parse_repo_url(repo["repo_url"])
        issue = await fetch_issue(owner, repo_name, issue_number, github_token)

        # Build detailed prompt
        prompt_parts = [
            f"# Fix GitHub Issue #{issue.number}",
            f"",
            f"**URL**: {issue.url}",
            f"**Author**: {issue.author}",
            f"**Labels**: {', '.join(issue.labels) if issue.labels else 'none'}",
            f"",
            f"## Title",
            f"{issue.title}",
            f"",
            f"## Description",
            f"{issue.body}",
        ]

        if issue.comments:
            prompt_parts.append("")
            prompt_parts.append("## Comments")
            for i, comment in enumerate(issue.comments, 1):
                prompt_parts.append(f"### Comment {i}")
                prompt_parts.append(comment)
                prompt_parts.append("")

        prompt_parts.extend([
            "",
            "## Your Task",
            "",
            "1. Understand the issue thoroughly",
            "2. Reproduce the bug or understand the feature request",
            "3. Implement a minimal, targeted fix",
            "4. Run tests to verify the fix works",
            "5. Commit your changes with a clear message",
            "6. Create a pull request using `gh pr create`",
            "",
            f"Use branch name: `repocraft/fix/{issue_number}-{_slugify(issue.title)}`",
            "",
            "The PR should include:",
            "- Clear title referencing the issue",
            "- Description of what changed and why",
            "- Test output showing the fix works",
        ])

        return "\n".join(prompt_parts)

    elif kind == "task":
        # Free-form instruction from user
        return trigger or "Execute the task as described."

    elif kind == "scan":
        return """\
Perform a comprehensive audit of this codebase. Look for:

1. **Security Issues**
   - Hardcoded secrets or credentials
   - SQL injection vulnerabilities
   - XSS vulnerabilities
   - Insecure dependencies (check for CVEs)
   - Unsafe file operations

2. **Code Quality**
   - Obvious bugs or logic errors
   - Dead code or unused imports
   - Code smells (long functions, deep nesting, etc.)
   - Missing error handling

3. **Dependencies**
   - Outdated packages (major versions behind)
   - Deprecated dependencies
   - Unused dependencies

4. **Testing**
   - Missing test coverage for critical paths
   - Flaky or broken tests

5. **Documentation**
   - Missing or outdated README
   - Undocumented public APIs
   - Missing inline comments for complex logic

For each finding:
- Create a GitHub issue with clear description and severity label
- OR if it's a simple fix, fix it directly and create a PR

Before creating issues, search existing issues to avoid duplicates.
"""

    elif kind == "respond_review":
        return f"""\
A pull request has received review comments. Your task:

1. Read all review comments carefully
2. Address each comment by either:
   - Fixing the code as requested
   - Replying to explain why the current approach is correct
3. Push new commits to the PR branch
4. Do NOT force-push unless explicitly requested by reviewer

Review comments:
{trigger}
"""

    elif kind == "triage":
        return f"""\
A new issue has been created. Your task:

1. Read the issue carefully
2. Determine if it's a bug, feature request, question, or duplicate
3. Reply with a brief, helpful comment
4. Apply appropriate labels if you have permission
5. If it's a duplicate, link to the original issue

Issue content:
{trigger}
"""

    else:
        raise ValueError(f"Unknown activity kind: {kind}")


async def _execute_claude(
    activity_id: str,
    repo_id: str,
    prompt: str,
    store: Store,
    container_mgr: ContainerManager,
    max_turns: int,
    verbose: bool,
) -> int:
    """Execute claude -p command and stream output to DB. Returns exit code."""
    # Base64 encode prompt to avoid shell escaping issues
    prompt_b64 = base64.b64encode(prompt.encode()).decode()

    # Build claude command
    cmd_parts = [
        f"cd /repos/{repo_id}",
        f"claude -p \"$(echo {prompt_b64} | base64 -d)\"",
        "--output-format stream-json",
        "--dangerously-skip-permissions",
        f"--max-turns {max_turns}",
    ]
    if verbose:
        cmd_parts.append("--verbose")

    command = " && ".join([cmd_parts[0], " ".join(cmd_parts[1:])])

    logger.debug("Executing: %s", command[:200])

    # Stream output and log each line
    line_count = 0
    exit_code = 1  # Default to failure
    async for line, code in container_mgr.exec_stream(command, workdir=f"/repos/{repo_id}"):
        if line is not None:
            store.add_log(activity_id, line)
            line_count += 1
            if line_count % 100 == 0:
                logger.debug("Logged %d lines for activity %s", line_count, activity_id)
        else:
            # Final tuple with exit code
            exit_code = code or 1

    logger.info("Claude command completed with exit code %d (%d lines logged)", exit_code, line_count)
    return exit_code


def _extract_summary(logs: list) -> str:
    """Extract summary from last few log lines (max 500 chars)."""
    if not logs:
        return "No output"

    # Try to parse stream-json from last 10 lines
    for log in reversed(logs[-10:]):
        line = log["line"]
        try:
            obj = json.loads(line)
            # Claude Code stream-json uses "result" field for final output
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
    """Convert text to URL-safe slug."""
    slug = text.lower()
    slug = "".join(c if c.isalnum() or c in ("-", "_") else "-" for c in slug)
    slug = "-".join(filter(None, slug.split("-")))  # Remove consecutive dashes
    return slug[:50]  # Limit length
