"""Issue indexer: batch/incremental embedding of GitHub issues for patrol deduplication."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .embeddings import generate_embedding, normalize_issue_summary

if TYPE_CHECKING:
    from .store import Store

logger = logging.getLogger(__name__)

# Rate limiting: pause between API calls
_RATE_LIMIT_DELAY_SECS = 0.5


async def _fetch_issues_from_github(
    owner: str, repo: str, github_token: str, state: str = "open"
) -> list[dict]:
    """Fetch issues from GitHub API."""
    import httpx

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    issues = []
    page = 1
    async with httpx.AsyncClient() as client:
        while True:
            resp = await client.get(
                f"https://api.github.com/repos/{owner}/{repo}/issues",
                params={"state": state, "per_page": 100, "page": page},
                headers=headers,
                timeout=15.0,
            )
            if resp.status_code != 200:
                logger.warning(
                    "Failed to fetch issues page %d for %s/%s: %s", page, owner, repo, resp.status_code
                )
                break
            batch = resp.json()
            if not batch:
                break
            # Exclude pull requests (GitHub API includes them in /issues)
            issues.extend(i for i in batch if "pull_request" not in i)
            if len(batch) < 100:
                break
            page += 1
            await asyncio.sleep(_RATE_LIMIT_DELAY_SECS)
    return issues


async def _fetch_issue_comments(
    owner: str, repo: str, issue_number: int, github_token: str
) -> list[str]:
    """Fetch comments for a specific issue."""
    import httpx

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments",
            params={"per_page": 20},
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code != 200:
            return []
        return [c.get("body", "") for c in resp.json()]


async def _index_issue(
    repo_id: str,
    issue: dict,
    owner: str,
    repo: str,
    github_token: str,
    store: "Store",
) -> None:
    """Index a single issue: normalize summary + generate embedding + upsert."""
    issue_number = issue["number"]
    title = issue.get("title", "")
    body = issue.get("body", "") or ""
    html_url = issue.get("html_url", "")
    state = issue.get("state", "open")

    # Fetch comments
    comments = await _fetch_issue_comments(owner, repo, issue_number, github_token)

    # Normalize summary (cheap Haiku call)
    summary = await normalize_issue_summary(title, body, comments)

    # Generate embedding from summary + title
    embedding_text = f"{title}\n\n{summary}"
    embedding = await generate_embedding(embedding_text)

    # Extract file paths from summary if they contain "/"
    file_paths_list = []
    for word in summary.split():
        if "/" in word and not word.startswith("http"):
            fp = word.strip(".,;:()\"'")
            if fp:
                file_paths_list.append(fp)

    file_paths_str = ",".join(file_paths_list) if file_paths_list else None

    # Determine source: CatoCode patrol issues have a specific body pattern
    source = "catocode" if "found by" in body.lower() and "patrol" in body.lower() else "human"

    store.upsert_issue_embedding(
        repo_id=repo_id,
        issue_number=issue_number,
        title=title,
        summary=summary,
        embedding=embedding,
        source=source,
        file_paths=file_paths_str,
        url=html_url,
    )

    # Update status if closed
    if state == "closed":
        store.update_issue_status(repo_id, issue_number, "closed")

    logger.debug("Indexed issue #%d for %s (source=%s)", issue_number, repo_id, source)
    await asyncio.sleep(_RATE_LIMIT_DELAY_SECS)


async def index_repo_issues(
    repo_id: str,
    owner: str,
    repo: str,
    github_token: str,
    store: "Store",
) -> int:
    """Batch-index all open issues for a repo on first-time setup.

    Returns number of issues indexed.
    """
    logger.info("Starting batch issue indexing for %s/%s (repo_id=%s)", owner, repo, repo_id)

    issues = await _fetch_issues_from_github(owner, repo, github_token, state="all")
    logger.info("Found %d issues to index for %s", len(issues), repo_id)

    count = 0
    for issue in issues:
        try:
            await _index_issue(repo_id, issue, owner, repo, github_token, store)
            count += 1
        except Exception as e:
            logger.warning("Failed to index issue #%d: %s", issue.get("number"), e)

    logger.info("Indexed %d/%d issues for %s", count, len(issues), repo_id)
    return count


async def index_single_issue(
    repo_id: str,
    issue_number: int,
    owner: str,
    repo: str,
    github_token: str,
    store: "Store",
) -> None:
    """Incrementally index a single issue (triggered by webhook)."""
    import httpx

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}",
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code != 200:
            logger.warning("Failed to fetch issue #%d for indexing: %s", issue_number, resp.status_code)
            return
        issue = resp.json()

    try:
        await _index_issue(repo_id, issue, owner, repo, github_token, store)
    except Exception as e:
        logger.warning("Failed to index issue #%d: %s", issue_number, e)


async def find_duplicates(
    repo_id: str,
    issue_description: str,
    store: "Store",
) -> list[dict]:
    """Two-stage dedup: vector recall + Haiku judgment.

    Returns list of {issue_number, url, similarity, verdict} for related/duplicate issues.
    """
    # Stage 1: Generate embedding for the new issue description
    query_embedding = await generate_embedding(issue_description)

    if query_embedding is None:
        # Fallback: just return recent open issues without similarity scores
        rows = store.get_open_issue_embeddings(repo_id)
        return [
            {
                "issue_number": r["github_issue_number"],
                "url": r.get("github_issue_url", ""),
                "similarity": 0.0,
                "verdict": "unknown",
                "title": r["title"],
            }
            for r in rows[:5]
        ]

    # Stage 2: Vector similarity search (top-5)
    candidates = store.search_similar_issues(repo_id, query_embedding, top_k=5)

    if not candidates:
        return []

    # Stage 3: Haiku judgment for each candidate
    results = []
    for candidate in candidates:
        verdict = await _haiku_judge_duplicate(
            issue_description,
            candidate.get("normalized_summary") or candidate["title"],
            candidate.get("file_paths") or "",
        )
        results.append({
            "issue_number": candidate["github_issue_number"],
            "url": candidate.get("github_issue_url", ""),
            "similarity": candidate.get("similarity", 0.0),
            "verdict": verdict,
            "title": candidate["title"],
        })

    # Return only duplicate or related
    return [r for r in results if r["verdict"] in ("duplicate", "related")]


async def _haiku_judge_duplicate(
    new_issue: str,
    existing_summary: str,
    existing_file_paths: str,
) -> str:
    """Use Haiku to judge if two issues describe the same bug.

    Returns: 'duplicate' | 'related' | 'unrelated'
    """
    import anthropic
    import os
    from .embeddings import SUMMARY_MODEL

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "unknown"

    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url

    prompt = f"""Determine if these two issues describe the same bug or problem.

New issue:
{new_issue[:500]}

Existing issue summary:
{existing_summary[:500]}

Existing issue file paths: {existing_file_paths or "(unknown)"}

Consider:
1. Are they in the same file/component?
2. Do they have the same root cause?
3. Even if similar type, could they be different instances in different locations?

Respond with exactly one word: "duplicate", "related", or "unrelated"
- duplicate: same bug, same location
- related: same bug type but different location/component
- unrelated: different problems"""

    try:
        client = anthropic.Anthropic(**client_kwargs)
        message = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        verdict = message.content[0].text.strip().lower()
        if verdict in ("duplicate", "related", "unrelated"):
            return verdict
        return "unrelated"
    except Exception as e:
        logger.warning("Haiku duplicate judgment failed: %s", e)
        return "unknown"
