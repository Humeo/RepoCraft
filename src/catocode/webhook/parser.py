"""GitHub webhook payload parser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class WebhookEvent:
    """Normalized webhook event."""

    event_id: str  # X-GitHub-Delivery header
    event_type: str  # Normalized type (issue_opened, pr_opened, etc.)
    repo_id: str
    trigger: str  # e.g., "issue:123", "pr:456"
    payload: dict[str, Any]
    actor: str  # GitHub username who triggered the event


def parse_webhook(
    event_name: str,
    payload: dict[str, Any],
    delivery_id: str,
    repo_id: str,
) -> WebhookEvent | None:
    """Parse GitHub webhook payload into normalized event.

    Args:
        event_name: X-GitHub-Event header (e.g., "issues", "pull_request")
        payload: Parsed JSON payload
        delivery_id: X-GitHub-Delivery header (unique event ID)
        repo_id: Repository ID (owner-repo format)

    Returns:
        WebhookEvent if the event should be processed, None otherwise
    """
    action = payload.get("action")
    sender = payload.get("sender", {}).get("login", "unknown")

    # Issues events
    if event_name == "issues":
        issue_number = payload.get("issue", {}).get("number")
        if not issue_number:
            return None

        if action == "opened":
            return WebhookEvent(
                event_id=delivery_id,
                event_type="issue_opened",
                repo_id=repo_id,
                trigger=f"issue:{issue_number}",
                payload=payload,
                actor=sender,
            )
        elif action in ("closed", "reopened"):
            # Track but don't create activities for these
            return None

    # Pull request events
    elif event_name == "pull_request":
        pr_number = payload.get("pull_request", {}).get("number")
        if not pr_number:
            return None

        if action == "opened":
            return WebhookEvent(
                event_id=delivery_id,
                event_type="pr_opened",
                repo_id=repo_id,
                trigger=f"pr:{pr_number}",
                payload=payload,
                actor=sender,
            )
        elif action == "synchronize":
            # New commits pushed to PR
            return WebhookEvent(
                event_id=delivery_id,
                event_type="pr_updated",
                repo_id=repo_id,
                trigger=f"pr:{pr_number}",
                payload=payload,
                actor=sender,
            )

    # Issue/PR comment events
    elif event_name == "issue_comment":
        comment = payload.get("comment", {})
        comment_body = comment.get("body", "")
        comment_id = comment.get("id")

        issue = payload.get("issue", {})
        issue_number = issue.get("number")

        if not issue_number or not comment_id:
            return None

        # Determine if this is a PR or issue comment
        is_pr = "pull_request" in issue
        trigger_prefix = "pr" if is_pr else "issue"

        if action == "created":
            # Check for @catocode mention or approval keywords
            if "@catocode" in comment_body.lower() or any(
                keyword in comment_body.lower()
                for keyword in ["/approve", "/fix", "go ahead"]
            ):
                return WebhookEvent(
                    event_id=delivery_id,
                    event_type="comment_created",
                    repo_id=repo_id,
                    trigger=f"{trigger_prefix}:{issue_number}:comment:{comment_id}",
                    payload=payload,
                    actor=sender,
                )

    # Pull request review events
    elif event_name == "pull_request_review":
        pr_number = payload.get("pull_request", {}).get("number")
        review = payload.get("review", {})  # noqa: F841

        if not pr_number:
            return None

        if action == "submitted":
            return WebhookEvent(
                event_id=delivery_id,
                event_type="pr_review_submitted",
                repo_id=repo_id,
                trigger=f"pr:{pr_number}",
                payload=payload,
                actor=sender,
            )

    return None
