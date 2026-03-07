from __future__ import annotations

from dataclasses import dataclass, field

import httpx


@dataclass
class GitHubIssue:
    number: int
    title: str
    body: str
    state: str
    author: str
    labels: list[str]
    comments: list[str]
    url: str
    created_at: str = ""


async def fetch_issue(
    owner: str,
    repo: str,
    issue_number: int,
    github_token: str | None = None,
) -> GitHubIssue:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    base = "https://api.github.com"
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        resp = await client.get(f"{base}/repos/{owner}/{repo}/issues/{issue_number}")
        resp.raise_for_status()
        data = resp.json()

        comments: list[str] = []
        if data.get("comments", 0) > 0:
            c_resp = await client.get(
                f"{base}/repos/{owner}/{repo}/issues/{issue_number}/comments",
                params={"per_page": 100},
            )
            c_resp.raise_for_status()
            comments = [c["body"] for c in c_resp.json() if c.get("body")]

    return GitHubIssue(
        number=data["number"],
        title=data["title"],
        body=data.get("body") or "",
        state=data["state"],
        author=data["user"]["login"],
        labels=[lb["name"] for lb in data.get("labels", [])],
        comments=comments,
        url=data["html_url"],
        created_at=data.get("created_at", ""),
    )
