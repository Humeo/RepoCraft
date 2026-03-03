from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def get_anthropic_api_key() -> str:
    """Get Anthropic API key from ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN."""
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN environment variable not set")
    return key


def get_anthropic_base_url() -> str | None:
    """Get custom Anthropic API base URL if set."""
    return os.environ.get("ANTHROPIC_BASE_URL")


def get_github_token() -> str:
    """Get GitHub token from GITHUB_TOKEN env var or gh CLI."""
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Parse GitHub issue URL into (owner, repo, issue_number)."""
    match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)",
        url.strip(),
    )
    if not match:
        raise ValueError(
            f"Invalid GitHub issue URL: {url!r}\n"
            "Expected format: https://github.com/owner/repo/issues/NUMBER"
        )
    return match.group(1), match.group(2), int(match.group(3))


def repo_id_from_url(repo_url: str) -> str:
    """Convert repo URL to repo_id slug (owner-repo)."""
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/?", repo_url.strip())
    if not match:
        raise ValueError(f"Invalid GitHub repo URL: {repo_url!r}")
    owner = match.group(1)
    repo = match.group(2).removesuffix(".git")
    return f"{owner}-{repo}"


def parse_repo_url(repo_url: str) -> tuple[str, str]:
    """Parse GitHub repo URL into (owner, repo)."""
    match = re.match(r"https?://github\.com/([^/]+)/([^/]+)/?", repo_url.strip())
    if not match:
        raise ValueError(f"Invalid GitHub repo URL: {repo_url!r}")
    owner = match.group(1)
    repo = match.group(2).removesuffix(".git")
    return owner, repo


# Backward compatibility for existing tests
@dataclass
class RepoCraftConfig:
    repo_url: str
    issue_number: int
    owner: str = field(init=False)
    repo: str = field(init=False)
    container_image: str = "repocraft-worker:v1"
    model: str = "claude-sonnet-4-6"
    max_turns_per_phase: int = 30
    max_budget_usd: float = 10.0
    output_dir: Path = field(default_factory=lambda: Path("output"))
    github_token: str | None = field(
        default_factory=lambda: os.environ.get("GITHUB_TOKEN")
    )

    def __post_init__(self) -> None:
        self.owner, self.repo = parse_repo_url(self.repo_url)
        self.output_dir = Path(self.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
