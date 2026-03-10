from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import Connection, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    github_id INTEGER NOT NULL UNIQUE,
    github_login TEXT NOT NULL,
    github_email TEXT,
    avatar_url TEXT,
    access_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_login_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
    watch INTEGER DEFAULT 0,
    last_etag TEXT,
    last_poll_at TEXT,
    patrol_interval_hours INTEGER DEFAULT 12,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    trigger TEXT,
    status TEXT DEFAULT 'pending',
    session_id TEXT,
    summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id TEXT NOT NULL,
    line TEXT NOT NULL,
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
    repo_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_type TEXT,
    processed_at TEXT NOT NULL,
    UNIQUE(repo_id, event_id)
);

CREATE TABLE IF NOT EXISTS patrol_budget (
    repo_id TEXT PRIMARY KEY,
    window_start TEXT NOT NULL,
    issues_filed INTEGER DEFAULT 0,
    max_issues INTEGER DEFAULT 5,
    window_hours INTEGER DEFAULT 12
);

CREATE TABLE IF NOT EXISTS webhook_config (
    repo_id TEXT PRIMARY KEY,
    webhook_secret TEXT NOT NULL,
    webhook_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id TEXT PRIMARY KEY,
    repo_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT,
    received_at TEXT NOT NULL,
    processed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS installations (
    installation_id TEXT PRIMARY KEY,
    account_login TEXT NOT NULL,
    account_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS install_states (
    state TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    used INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS issue_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    github_issue_number INTEGER NOT NULL,
    title TEXT NOT NULL,
    normalized_summary TEXT,
    embedding TEXT,
    source TEXT DEFAULT 'human',
    status TEXT DEFAULT 'open',
    file_paths TEXT,
    github_issue_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(repo_id, github_issue_number)
);

CREATE TABLE IF NOT EXISTS patrol_reviewed_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    review_source TEXT NOT NULL,
    reviewed_at TEXT NOT NULL,
    UNIQUE(repo_id, file_path)
);
"""

# Migrations: columns added after initial schema
_MIGRATIONS = [
    "ALTER TABLE repos ADD COLUMN watch INTEGER DEFAULT 0",
    "ALTER TABLE repos ADD COLUMN last_etag TEXT",
    "ALTER TABLE repos ADD COLUMN last_poll_at TEXT",
    "ALTER TABLE repos ADD COLUMN patrol_interval_hours INTEGER DEFAULT 12",
    "ALTER TABLE activities ADD COLUMN requires_approval INTEGER DEFAULT 0",
    "ALTER TABLE activities ADD COLUMN approval_comment_url TEXT",
    "ALTER TABLE activities ADD COLUMN approved_by TEXT",
    "ALTER TABLE activities ADD COLUMN approved_at TEXT",
    "ALTER TABLE activities ADD COLUMN cost_usd REAL",
    "ALTER TABLE repos ADD COLUMN user_id TEXT",
    "ALTER TABLE installations ADD COLUMN user_id TEXT",
    "ALTER TABLE repos ADD COLUMN patrol_enabled INTEGER DEFAULT 0",
    "ALTER TABLE repos ADD COLUMN patrol_max_issues INTEGER DEFAULT 5",
    "ALTER TABLE repos ADD COLUMN patrol_window_hours INTEGER DEFAULT 12",
    "ALTER TABLE repos ADD COLUMN last_patrol_sha TEXT",
    "ALTER TABLE activities ADD COLUMN metadata TEXT",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(
        self,
        db_path: Path | None = None,
        db_url: str | None = None,
    ) -> None:
        if db_url:
            self._db: Connection = connect(db_url)
        elif db_path:
            self._db = connect(str(db_path))
        else:
            self._db = connect()
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Apply schema migrations idempotently."""
        _logger = logging.getLogger(__name__)
        for migration in _MIGRATIONS:
            try:
                self._db.execute(migration)
                self._db.commit()
            except Exception as e:
                err_lower = str(e).lower()
                # Ignore expected idempotency errors (column/table already exists)
                if any(k in err_lower for k in ("duplicate column", "already exists", "duplicate key")):
                    pass
                else:
                    _logger.warning("Migration skipped with unexpected error: %s | SQL: %.120s", e, migration)

    # --- repos ---

    def add_repo(self, repo_id: str, repo_url: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO repos (id, repo_url, created_at) VALUES (?, ?, ?)",
            (repo_id, repo_url, _now()),
        )
        self._db.commit()

    def get_repo(self, repo_id: str) -> dict | None:
        return self._db.execute_one("SELECT * FROM repos WHERE id = ?", (repo_id,))

    def list_repos(self, user_id: str | None = None) -> list[dict]:
        if user_id is not None:
            return self._db.execute(
                "SELECT * FROM repos WHERE user_id = ? ORDER BY created_at", (user_id,)
            )
        return self._db.execute("SELECT * FROM repos ORDER BY created_at")

    def list_watched_repos(self) -> list[dict]:
        return self._db.execute("SELECT * FROM repos WHERE watch = 1 ORDER BY created_at")

    def update_repo(self, repo_id: str, **fields: object) -> None:
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = tuple(fields.values()) + (repo_id,)
        self._db.execute(f"UPDATE repos SET {set_clause} WHERE id = ?", values)
        self._db.commit()

    def delete_repo(self, repo_id: str) -> None:
        self._db.execute("DELETE FROM repos WHERE id = ?", (repo_id,))
        self._db.commit()

    # --- activities ---

    def add_activity(self, repo_id: str, kind: str, trigger: str | None = None, metadata: dict | None = None) -> str:
        import json as _json
        activity_id = str(uuid.uuid4())
        now = _now()
        metadata_str = _json.dumps(metadata) if metadata else None
        self._db.execute(
            """INSERT INTO activities
               (id, repo_id, kind, trigger, status, metadata, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (activity_id, repo_id, kind, trigger, metadata_str, now, now),
        )
        self._db.commit()
        return activity_id

    def get_activity(self, activity_id: str) -> dict | None:
        return self._db.execute_one(
            "SELECT * FROM activities WHERE id = ?", (activity_id,)
        )

    def update_activity(self, activity_id: str, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = tuple(fields.values()) + (activity_id,)
        self._db.execute(f"UPDATE activities SET {set_clause} WHERE id = ?", values)
        self._db.commit()

    def get_pending_activities(self) -> list[dict]:
        return self._db.execute(
            "SELECT * FROM activities WHERE status = 'pending' ORDER BY created_at"
        )

    def get_running_activities(self) -> list[dict]:
        return self._db.execute(
            "SELECT * FROM activities WHERE status = 'running' ORDER BY created_at"
        )

    def list_activities(self, repo_id: str | None = None, user_id: str | None = None) -> list[dict]:
        if repo_id is not None:
            return self._db.execute(
                "SELECT * FROM activities WHERE repo_id = ? ORDER BY created_at",
                (repo_id,),
            )
        if user_id is not None:
            return self._db.execute(
                "SELECT a.* FROM activities a JOIN repos r ON a.repo_id = r.id "
                "WHERE r.user_id = ? ORDER BY a.created_at",
                (user_id,),
            )
        return self._db.execute("SELECT * FROM activities ORDER BY created_at")

    def get_stats(self, user_id: str | None = None) -> dict:
        """Return aggregate stats for the dashboard."""
        if user_id is not None:
            repos_total = (self._db.execute_one(
                "SELECT COUNT(*) as c FROM repos WHERE user_id = ?", (user_id,)
            ) or {}).get("c", 0)
            repos_watched = (self._db.execute_one(
                "SELECT COUNT(*) as c FROM repos WHERE watch=1 AND user_id = ?", (user_id,)
            ) or {}).get("c", 0)
            rows = self._db.execute(
                "SELECT a.status, COUNT(*) as cnt, SUM(COALESCE(a.cost_usd,0)) as cost "
                "FROM activities a JOIN repos r ON a.repo_id = r.id "
                "WHERE r.user_id = ? GROUP BY a.status",
                (user_id,),
            )
            kind_rows = self._db.execute(
                "SELECT a.kind, COUNT(*) as cnt FROM activities a JOIN repos r ON a.repo_id = r.id "
                "WHERE r.user_id = ? GROUP BY a.kind ORDER BY cnt DESC",
                (user_id,),
            )
            recent = self._db.execute(
                "SELECT a.* FROM activities a JOIN repos r ON a.repo_id = r.id "
                "WHERE r.user_id = ? ORDER BY a.updated_at DESC LIMIT 20",
                (user_id,),
            )
        else:
            repos_total = (self._db.execute_one("SELECT COUNT(*) as c FROM repos") or {}).get("c", 0)
            repos_watched = (self._db.execute_one("SELECT COUNT(*) as c FROM repos WHERE watch=1") or {}).get("c", 0)
            rows = self._db.execute(
                "SELECT status, COUNT(*) as cnt, SUM(COALESCE(cost_usd,0)) as cost FROM activities GROUP BY status"
            )
            kind_rows = self._db.execute(
                "SELECT kind, COUNT(*) as cnt FROM activities GROUP BY kind ORDER BY cnt DESC"
            )
            recent = self._db.execute(
                "SELECT * FROM activities ORDER BY updated_at DESC LIMIT 20"
            )
        by_status: dict[str, int] = {}
        total_cost: float = 0.0
        for row in rows:
            by_status[row["status"]] = row["cnt"]
            total_cost += row["cost"] or 0.0

        by_kind = {r["kind"]: r["cnt"] for r in kind_rows}

        return {
            "repos": {"total": repos_total, "watched": repos_watched},
            "activities": {
                "by_status": by_status,
                "by_kind": by_kind,
                "total": sum(by_status.values()),
            },
            "cost_usd": round(total_cost, 4),
            "recent_activities": recent,
        }

    def get_repo_stats(self, repo_id: str) -> dict | None:
        """Return per-repo stats for the dashboard."""
        repo = self._db.execute_one("SELECT * FROM repos WHERE id=?", (repo_id,))
        if repo is None:
            return None

        rows = self._db.execute(
            "SELECT status, COUNT(*) as cnt, SUM(COALESCE(cost_usd,0)) as cost "
            "FROM activities WHERE repo_id=? GROUP BY status",
            (repo_id,),
        )
        by_status: dict[str, int] = {}
        total_cost: float = 0.0
        for row in rows:
            by_status[row["status"]] = row["cnt"]
            total_cost += row["cost"] or 0.0

        recent = self._db.execute(
            "SELECT * FROM activities WHERE repo_id=? ORDER BY updated_at DESC LIMIT 10",
            (repo_id,),
        )

        return {
            "repo": repo,
            "activities": {
                "by_status": by_status,
                "total": sum(by_status.values()),
            },
            "cost_usd": round(total_cost, 4),
            "recent_activities": recent,
        }

    def mark_crashed_activities_failed(self) -> int:
        """On daemon startup, mark any status=running activities as failed (previous crash)."""
        rows_before = self._db.execute(
            "SELECT COUNT(*) as c FROM activities WHERE status = 'running'"
        )
        count = (rows_before[0] if rows_before else {}).get("c", 0)
        self._db.execute(
            "UPDATE activities SET status = 'failed', "
            "summary = 'Interrupted (daemon restarted)', updated_at = ? "
            "WHERE status = 'running'",
            (_now(),),
        )
        self._db.commit()
        return count

    # --- logs ---

    def add_log(self, activity_id: str, line: str) -> None:
        self._db.execute(
            "INSERT INTO logs (activity_id, line, ts) VALUES (?, ?, ?)",
            (activity_id, line, _now()),
        )
        self._db.commit()

    def add_logs_batch(self, activity_id: str, lines: list[str]) -> None:
        """Batch-insert log lines for better I/O performance."""
        if not lines:
            return
        now = _now()
        self._db.executemany(
            "INSERT INTO logs (activity_id, line, ts) VALUES (?, ?, ?)",
            [(activity_id, line, now) for line in lines],
        )
        self._db.commit()

    def get_logs(self, activity_id: str) -> list[dict]:
        return self._db.execute(
            "SELECT * FROM logs WHERE activity_id = ? ORDER BY id",
            (activity_id,),
        )

    def get_logs_after(self, activity_id: str, after_id: int = 0) -> list[dict]:
        """Return log rows with id > after_id (for incremental / SSE streaming)."""
        return self._db.execute(
            "SELECT * FROM logs WHERE activity_id = ? AND id > ? ORDER BY id",
            (activity_id, after_id),
        )

    # --- processed events ---

    def is_event_processed(self, repo_id: str, event_id: str) -> bool:
        row = self._db.execute_one(
            "SELECT 1 as found FROM processed_events WHERE repo_id = ? AND event_id = ?",
            (repo_id, event_id),
        )
        return row is not None

    def mark_event_processed(self, repo_id: str, event_id: str, event_type: str) -> None:
        self._db.execute(
            """INSERT OR IGNORE INTO processed_events
               (repo_id, event_id, event_type, processed_at)
               VALUES (?, ?, ?, ?)""",
            (repo_id, event_id, event_type, _now()),
        )
        self._db.commit()

    # --- patrol budget ---

    def get_patrol_budget(self, repo_id: str) -> int:
        """Return remaining issues budget for this patrol window. Resets when window expires."""
        row = self._db.execute_one(
            "SELECT * FROM patrol_budget WHERE repo_id = ?", (repo_id,)
        )
        if row is None:
            return 5  # Default budget for new repos

        window_start = datetime.fromisoformat(row["window_start"])
        window_hours = row["window_hours"]
        elapsed_hours = (datetime.now(timezone.utc) - window_start).total_seconds() / 3600

        if elapsed_hours >= window_hours:
            self._db.execute(
                "UPDATE patrol_budget SET window_start = ?, issues_filed = 0 WHERE repo_id = ?",
                (_now(), repo_id),
            )
            self._db.commit()
            return row["max_issues"]

        return max(0, row["max_issues"] - row["issues_filed"])

    def init_patrol_budget(self, repo_id: str, max_issues: int = 5, window_hours: int = 12) -> None:
        """Initialize patrol budget for a repo (idempotent)."""
        self._db.execute(
            """INSERT OR IGNORE INTO patrol_budget
               (repo_id, window_start, issues_filed, max_issues, window_hours)
               VALUES (?, ?, 0, ?, ?)""",
            (repo_id, _now(), max_issues, window_hours),
        )
        self._db.commit()

    def decrement_patrol_budget(self, repo_id: str) -> None:
        """Record that one issue was filed in the current patrol window."""
        self._db.execute(
            "UPDATE patrol_budget SET issues_filed = issues_filed + 1 WHERE repo_id = ?",
            (repo_id,),
        )
        self._db.commit()

    # --- approval workflow ---

    def get_pending_approval_activities(self) -> list[dict]:
        """Get activities waiting for human approval."""
        return self._db.execute(
            """SELECT * FROM activities
               WHERE status = 'pending' AND requires_approval = 1
               ORDER BY created_at"""
        )

    # --- webhook management ---

    def add_webhook_config(self, repo_id: str, secret: str, webhook_id: str | None = None) -> None:
        """Store webhook configuration for a repo."""
        self._db.execute(
            """INSERT OR REPLACE INTO webhook_config
               (repo_id, webhook_secret, webhook_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (repo_id, secret, webhook_id, _now()),
        )
        self._db.commit()

    def get_webhook_config(self, repo_id: str) -> dict | None:
        """Get webhook configuration for a repo."""
        return self._db.execute_one(
            "SELECT * FROM webhook_config WHERE repo_id = ?", (repo_id,)
        )

    def add_webhook_event(
        self, event_id: str, repo_id: str, event_type: str, payload: str | None = None
    ) -> None:
        """Record a received webhook event."""
        self._db.execute(
            """INSERT OR IGNORE INTO webhook_events
               (event_id, repo_id, event_type, payload, received_at)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, repo_id, event_type, payload, _now()),
        )
        self._db.commit()

    def is_webhook_event_processed(self, event_id: str) -> bool:
        """Check if a webhook event has been processed."""
        row = self._db.execute_one(
            "SELECT processed FROM webhook_events WHERE event_id = ?", (event_id,)
        )
        return row is not None and row["processed"] == 1

    def mark_webhook_event_processed(self, event_id: str) -> None:
        """Mark a webhook event as processed."""
        self._db.execute(
            "UPDATE webhook_events SET processed = 1 WHERE event_id = ?", (event_id,)
        )
        self._db.commit()

    # --- GitHub App installations ---

    def add_installation(
        self,
        installation_id: str,
        account_login: str,
        account_type: str,
    ) -> None:
        """Record a GitHub App installation."""
        self._db.execute(
            """INSERT OR REPLACE INTO installations
               (installation_id, account_login, account_type, created_at)
               VALUES (?, ?, ?, ?)""",
            (installation_id, account_login, account_type, _now()),
        )
        self._db.commit()

    def get_installation(self, installation_id: str) -> dict | None:
        """Get a GitHub App installation record."""
        return self._db.execute_one(
            "SELECT * FROM installations WHERE installation_id = ?", (installation_id,)
        )

    def delete_installation(self, installation_id: str) -> None:
        """Remove a GitHub App installation record."""
        self._db.execute(
            "DELETE FROM installations WHERE installation_id = ?", (installation_id,)
        )
        self._db.commit()

    def link_installation_to_user(self, installation_id: str, user_id: str) -> None:
        """Associate a GitHub App installation with a user."""
        self._db.execute(
            "UPDATE installations SET user_id = ? WHERE installation_id = ?",
            (user_id, installation_id),
        )
        self._db.commit()

    def get_user_id_for_installation(self, installation_id: str) -> str | None:
        """Return user_id associated with an installation, or None."""
        row = self._db.execute_one(
            "SELECT user_id FROM installations WHERE installation_id = ?", (installation_id,)
        )
        return row["user_id"] if row else None

    # --- users ---

    def create_user(
        self,
        user_id: str,
        github_id: int,
        github_login: str,
        github_email: str | None,
        avatar_url: str | None,
        access_token: str,
    ) -> None:
        now = _now()
        self._db.execute(
            """INSERT INTO users (id, github_id, github_login, github_email, avatar_url,
               access_token, created_at, last_login_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, github_id, github_login, github_email, avatar_url, access_token, now, now),
        )
        self._db.commit()

    def get_user_by_github_id(self, github_id: int) -> dict | None:
        return self._db.execute_one(
            "SELECT * FROM users WHERE github_id = ?", (github_id,)
        )

    def get_user(self, user_id: str) -> dict | None:
        return self._db.execute_one("SELECT * FROM users WHERE id = ?", (user_id,))

    def update_user_last_login(self, user_id: str, access_token: str) -> None:
        self._db.execute(
            "UPDATE users SET last_login_at = ?, access_token = ? WHERE id = ?",
            (_now(), access_token, user_id),
        )
        self._db.commit()

    # --- sessions ---

    def create_session(self, token: str, user_id: str, expires_at: str) -> None:
        self._db.execute(
            "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (token, user_id, expires_at, _now()),
        )
        self._db.commit()

    def get_session(self, token: str) -> dict | None:
        return self._db.execute_one(
            "SELECT * FROM sessions WHERE token = ?", (token,)
        )

    def delete_session(self, token: str) -> None:
        self._db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        self._db.commit()

    # --- oauth CSRF states ---

    def create_oauth_state(self, state: str) -> None:
        self._db.execute(
            "INSERT INTO oauth_states (state, created_at, used) VALUES (?, ?, 0)",
            (state, _now()),
        )
        self._db.commit()

    def consume_oauth_state(self, state: str) -> bool:
        """Validate and consume a CSRF nonce. Returns True if valid (not used, <10 min old)."""
        from datetime import timedelta
        row = self._db.execute_one(
            "SELECT * FROM oauth_states WHERE state = ? AND used = 0", (state,)
        )
        if row is None:
            return False
        created = datetime.fromisoformat(row["created_at"])
        if datetime.now(timezone.utc) - created > timedelta(minutes=10):
            return False
        self._db.execute("UPDATE oauth_states SET used = 1 WHERE state = ?", (state,))
        self._db.commit()
        return True

    # --- install CSRF states ---

    def create_install_state(self, state: str, user_id: str) -> None:
        """Store a CSRF nonce that maps to a user_id for the GitHub App install flow."""
        self._db.execute(
            "INSERT INTO install_states (state, user_id, created_at, used) VALUES (?, ?, ?, 0)",
            (state, user_id, _now()),
        )
        self._db.commit()

    def consume_install_state(self, state: str) -> str | None:
        """Validate and consume an install CSRF nonce. Returns user_id if valid, else None."""
        from datetime import timedelta
        row = self._db.execute_one(
            "SELECT * FROM install_states WHERE state = ? AND used = 0", (state,)
        )
        if row is None:
            return None
        created = datetime.fromisoformat(row["created_at"])
        if datetime.now(timezone.utc) - created > timedelta(minutes=10):
            return None
        self._db.execute("UPDATE install_states SET used = 1 WHERE state = ?", (state,))
        self._db.commit()
        return row["user_id"]

    # --- patrol reviewed files ---

    def upsert_reviewed_file(
        self, repo_id: str, file_path: str, commit_sha: str, review_source: str
    ) -> None:
        """Record (or update) a file review entry."""
        self._db.execute(
            """INSERT INTO patrol_reviewed_files
               (repo_id, file_path, commit_sha, review_source, reviewed_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(repo_id, file_path) DO UPDATE SET
                 commit_sha = excluded.commit_sha,
                 review_source = excluded.review_source,
                 reviewed_at = excluded.reviewed_at""",
            (repo_id, file_path, commit_sha, review_source, _now()),
        )
        self._db.commit()

    def get_reviewed_files(self, repo_id: str) -> list[dict]:
        """Return all reviewed file records for a repo."""
        return self._db.execute(
            "SELECT * FROM patrol_reviewed_files WHERE repo_id = ?", (repo_id,)
        )

    # --- issue embeddings ---

    def upsert_issue_embedding(
        self,
        repo_id: str,
        issue_number: int,
        title: str,
        summary: str | None,
        embedding: list[float] | None,
        source: str,
        file_paths: str | None,
        url: str | None,
    ) -> None:
        """Insert or update an issue embedding record."""
        import json as _json
        embedding_str = _json.dumps(embedding) if embedding is not None else None
        now = _now()
        self._db.execute(
            """INSERT INTO issue_embeddings
               (repo_id, github_issue_number, title, normalized_summary, embedding,
                source, status, file_paths, github_issue_url, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
               ON CONFLICT(repo_id, github_issue_number) DO UPDATE SET
                 title = excluded.title,
                 normalized_summary = excluded.normalized_summary,
                 embedding = excluded.embedding,
                 source = excluded.source,
                 file_paths = excluded.file_paths,
                 github_issue_url = excluded.github_issue_url,
                 updated_at = excluded.updated_at""",
            (repo_id, issue_number, title, summary, embedding_str,
             source, file_paths, url, now, now),
        )
        self._db.commit()

    def update_issue_status(self, repo_id: str, issue_number: int, status: str) -> None:
        """Update the status of a tracked issue (e.g., 'closed')."""
        self._db.execute(
            "UPDATE issue_embeddings SET status = ?, updated_at = ? "
            "WHERE repo_id = ? AND github_issue_number = ?",
            (status, _now(), repo_id, issue_number),
        )
        self._db.commit()

    def get_open_issue_embeddings(self, repo_id: str) -> list[dict]:
        """Return all open issue embedding records for a repo."""
        import json as _json
        rows = self._db.execute(
            "SELECT * FROM issue_embeddings WHERE repo_id = ? AND status = 'open' ORDER BY updated_at DESC",
            (repo_id,),
        )
        result = []
        for row in rows:
            r = dict(row)
            if r.get("embedding"):
                try:
                    r["embedding"] = _json.loads(r["embedding"])
                except Exception:
                    r["embedding"] = None
            result.append(r)
        return result

    def search_similar_issues(
        self, repo_id: str, query_embedding: list[float], top_k: int = 5
    ) -> list[dict]:
        """Return top_k open issues with cosine similarity (SQLite fallback: recency order)."""
        import json as _json
        import math

        rows = self._db.execute(
            "SELECT * FROM issue_embeddings WHERE repo_id = ? AND status = 'open' "
            "AND embedding IS NOT NULL ORDER BY updated_at DESC LIMIT 50",
            (repo_id,),
        )

        def _cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            if na == 0 or nb == 0:
                return 0.0
            return dot / (na * nb)

        scored = []
        for row in rows:
            r = dict(row)
            try:
                emb = _json.loads(r["embedding"]) if r.get("embedding") else None
            except Exception:
                emb = None
            if emb:
                sim = _cosine(query_embedding, emb)
                r["similarity"] = sim
                r["embedding"] = emb
                scored.append(r)

        scored.sort(key=lambda x: x["similarity"], reverse=True)
        return scored[:top_k]

    def get_catocode_open_issue_files(self, repo_id: str) -> set[str]:
        """Return set of file paths mentioned in open CatoCode-filed issues."""
        rows = self._db.execute(
            "SELECT file_paths FROM issue_embeddings "
            "WHERE repo_id = ? AND status = 'open' AND source = 'catocode' AND file_paths IS NOT NULL",
            (repo_id,),
        )
        result: set[str] = set()
        for row in rows:
            for fp in (row["file_paths"] or "").split(","):
                fp = fp.strip()
                if fp:
                    result.add(fp)
        return result

    # --- patrol settings ---

    def update_patrol_settings(
        self,
        repo_id: str,
        enabled: bool,
        interval_hours: int,
        max_issues: int,
        window_hours: int,
    ) -> None:
        """Update patrol configuration for a repo."""
        self._db.execute(
            "UPDATE repos SET patrol_enabled = ?, patrol_interval_hours = ?, "
            "patrol_max_issues = ?, patrol_window_hours = ? WHERE id = ?",
            (1 if enabled else 0, interval_hours, max_issues, window_hours, repo_id),
        )
        self._db.commit()
        # Also update patrol_budget table
        self._db.execute(
            """INSERT INTO patrol_budget (repo_id, window_start, issues_filed, max_issues, window_hours)
               VALUES (?, ?, 0, ?, ?)
               ON CONFLICT(repo_id) DO UPDATE SET
                 max_issues = excluded.max_issues,
                 window_hours = excluded.window_hours""",
            (repo_id, _now(), max_issues, window_hours),
        )
        self._db.commit()

    def update_last_patrol_sha(self, repo_id: str, sha: str) -> None:
        """Record the HEAD SHA of the last patrol run."""
        self._db.execute(
            "UPDATE repos SET last_patrol_sha = ? WHERE id = ?", (sha, repo_id)
        )
        self._db.commit()
