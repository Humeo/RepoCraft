"""Embedding service for CatoCode patrol deduplication.

Uses OpenAI-compatible client pointing to yunwu.ai for:
- text-embedding-3-large embeddings (dimension: 3072)
- claude-haiku-4-5 for normalized issue summary generation
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.environ.get("EMBEDDING_BASE_URL", "https://yunwu.ai/v1")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-large")

# Haiku model for cheap summarization (override via SUMMARY_MODEL env var)
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-haiku-4-5-20251001")


def _get_openai_client():
    """Return async OpenAI client pointed at the configured base URL."""
    from openai import AsyncOpenAI
    if not EMBEDDING_API_KEY:
        return None
    return AsyncOpenAI(api_key=EMBEDDING_API_KEY, base_url=EMBEDDING_BASE_URL)


async def generate_embedding(text: str) -> list[float] | None:
    """Generate text embedding. Returns None on failure (graceful degradation)."""
    client = _get_openai_client()
    if client is None:
        logger.debug("Embedding service not configured, skipping embedding generation")
        return None

    try:
        response = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:8000],  # Truncate to avoid token limit issues
        )
        return response.data[0].embedding
    except Exception as e:
        logger.warning("Failed to generate embedding: %s", e)
        return None


async def normalize_issue_summary(title: str, body: str, comments: list[str]) -> str:
    """Use Claude Haiku to generate a normalized issue summary for deduplication.

    Extracts: bug type, affected module, root cause keywords, likely file paths.
    Returns the original title if Haiku is unavailable.
    """
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return title

    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    client_kwargs = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url

    comments_text = "\n".join(f"- {c[:200]}" for c in comments[:5])
    prompt = f"""Analyze this GitHub issue and produce a concise normalized summary for deduplication.

Issue title: {title}

Issue body:
{body[:1000]}

Comments (up to 20):
{comments_text or "(none)"}

Output a JSON object with these fields:
- bug_type: category (e.g., "null_pointer", "auth_bypass", "race_condition", "import_error")
- module: affected module or subsystem (e.g., "auth", "database", "api/routes")
- root_cause_keywords: 3-5 keywords describing the root cause
- file_paths: list of likely file paths involved (best guess, can be empty)
- one_line: one-sentence plain English summary

Output ONLY the JSON, no other text."""

    try:
        client = anthropic.Anthropic(**client_kwargs)
        message = client.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        import json
        raw = message.content[0].text.strip()
        data = json.loads(raw)
        parts = [
            data.get("bug_type", ""),
            data.get("module", ""),
            " ".join(data.get("root_cause_keywords", [])),
            data.get("one_line", title),
        ]
        return " | ".join(p for p in parts if p)
    except Exception as e:
        logger.warning("Failed to normalize issue summary: %s", e)
        return title


def is_embedding_service_configured() -> bool:
    """Check if embedding service is available."""
    return bool(EMBEDDING_API_KEY)


async def check_embedding_service() -> tuple[bool, str]:
    """Verify embedding service connectivity. Returns (ok, error_message)."""
    if not EMBEDDING_API_KEY:
        return False, "EMBEDDING_API_KEY not configured"
    try:
        result = await generate_embedding("test")
        if result is None:
            return False, "Embedding returned None"
        return True, ""
    except Exception as e:
        return False, str(e)
