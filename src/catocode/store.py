from __future__ import annotations

import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
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
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            env_path = os.environ.get("CATOCODE_DB_PATH")
            db_path = Path(env_path) if env_path else Path.home() / ".catocode" / "catocode.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Apply schema migrations idempotently (ignore errors for existing columns)."""
        for migration in _MIGRATIONS:
            try:
                self._conn.execute(migration)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

    # --- repos ---

    def add_repo(self, repo_id: str, repo_url: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO repos (id, repo_url, created_at) VALUES (?, ?, ?)",
                (repo_id, repo_url, _now()),
            )
            self._conn.commit()

    def get_repo(self, repo_id: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,))
            return cur.fetchone()

    def list_repos(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM repos ORDER BY created_at")
            return cur.fetchall()

    def list_watched_repos(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute("SELECT * FROM repos WHERE watch = 1 ORDER BY created_at")
            return cur.fetchall()

    def update_repo(self, repo_id: str, **fields: object) -> None:
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [repo_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE repos SET {set_clause} WHERE id = ?", values
            )
            self._conn.commit()

    # --- activities ---

    def add_activity(self, repo_id: str, kind: str, trigger: str | None = None) -> str:
        activity_id = str(uuid.uuid4())
        now = _now()
        with self._lock:
            self._conn.execute(
                """INSERT INTO activities
                   (id, repo_id, kind, trigger, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                (activity_id, repo_id, kind, trigger, now, now),
            )
            self._conn.commit()
        return activity_id

    def get_activity(self, activity_id: str) -> sqlite3.Row | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM activities WHERE id = ?", (activity_id,)
            )
            return cur.fetchone()

    def update_activity(self, activity_id: str, **fields: object) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [activity_id]
        with self._lock:
            self._conn.execute(
                f"UPDATE activities SET {set_clause} WHERE id = ?", values
            )
            self._conn.commit()

    def get_pending_activities(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM activities WHERE status = 'pending' ORDER BY created_at"
            )
            return cur.fetchall()

    def get_running_activities(self) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM activities WHERE status = 'running' ORDER BY created_at"
            )
            return cur.fetchall()

    def list_activities(self, repo_id: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if repo_id is not None:
                cur = self._conn.execute(
                    "SELECT * FROM activities WHERE repo_id = ? ORDER BY created_at",
                    (repo_id,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM activities ORDER BY created_at"
                )
            return cur.fetchall()

    def mark_crashed_activities_failed(self) -> int:
        """On daemon startup, mark any status=running activities as failed (previous crash)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE activities SET status = 'failed', summary = 'Interrupted (daemon restarted)'"
                ", updated_at = ? WHERE status = 'running'",
                (_now(),),
            )
            self._conn.commit()
            return cur.rowcount

    # --- logs ---

    def add_log(self, activity_id: str, line: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs (activity_id, line, ts) VALUES (?, ?, ?)",
                (activity_id, line, _now()),
            )
            self._conn.commit()

    def add_logs_batch(self, activity_id: str, lines: list[str]) -> None:
        """Batch-insert log lines for better I/O performance."""
        if not lines:
            return
        now = _now()
        rows = [(activity_id, line, now) for line in lines]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO logs (activity_id, line, ts) VALUES (?, ?, ?)",
                rows,
            )
            self._conn.commit()

    def get_logs(self, activity_id: str) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM logs WHERE activity_id = ? ORDER BY id",
                (activity_id,),
            )
            return cur.fetchall()

    # --- processed events ---

    def is_event_processed(self, repo_id: str, event_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM processed_events WHERE repo_id = ? AND event_id = ?",
                (repo_id, event_id),
            )
            return cur.fetchone() is not None

    def mark_event_processed(self, repo_id: str, event_id: str, event_type: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO processed_events
                   (repo_id, event_id, event_type, processed_at)
                   VALUES (?, ?, ?, ?)""",
                (repo_id, event_id, event_type, _now()),
            )
            self._conn.commit()

    # --- patrol budget ---

    def get_patrol_budget(self, repo_id: str) -> int:
        """Return remaining issues budget for this patrol window. Resets when window expires."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM patrol_budget WHERE repo_id = ?", (repo_id,)
            )
            row = cur.fetchone()

        if row is None:
            return 5  # Default budget for new repos

        # Check if window has expired
        window_start = datetime.fromisoformat(row["window_start"])
        window_hours = row["window_hours"]
        elapsed_hours = (datetime.now(timezone.utc) - window_start).total_seconds() / 3600

        if elapsed_hours >= window_hours:
            # Reset window
            with self._lock:
                self._conn.execute(
                    "UPDATE patrol_budget SET window_start = ?, issues_filed = 0 WHERE repo_id = ?",
                    (_now(), repo_id),
                )
                self._conn.commit()
            return row["max_issues"]

        return max(0, row["max_issues"] - row["issues_filed"])

    def init_patrol_budget(self, repo_id: str, max_issues: int = 5, window_hours: int = 12) -> None:
        """Initialize patrol budget for a repo (idempotent)."""
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO patrol_budget
                   (repo_id, window_start, issues_filed, max_issues, window_hours)
                   VALUES (?, ?, 0, ?, ?)""",
                (repo_id, _now(), max_issues, window_hours),
            )
            self._conn.commit()

    def decrement_patrol_budget(self, repo_id: str) -> None:
        """Record that one issue was filed in the current patrol window."""
        with self._lock:
            self._conn.execute(
                "UPDATE patrol_budget SET issues_filed = issues_filed + 1 WHERE repo_id = ?",
                (repo_id,),
            )
            self._conn.commit()

    # --- approval workflow ---

    def get_pending_approval_activities(self) -> list[sqlite3.Row]:
        """Get activities waiting for human approval."""
        with self._lock:
            cur = self._conn.execute(
                """SELECT * FROM activities
                   WHERE status = 'pending' AND requires_approval = 1
                   ORDER BY created_at"""
            )
            return cur.fetchall()

    # --- webhook management ---

    def add_webhook_config(self, repo_id: str, secret: str, webhook_id: str | None = None) -> None:
        """Store webhook configuration for a repo."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO webhook_config
                   (repo_id, webhook_secret, webhook_id, created_at)
                   VALUES (?, ?, ?, ?)""",
                (repo_id, secret, webhook_id, _now()),
            )
            self._conn.commit()

    def get_webhook_config(self, repo_id: str) -> sqlite3.Row | None:
        """Get webhook configuration for a repo."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM webhook_config WHERE repo_id = ?", (repo_id,)
            )
            return cur.fetchone()

    def add_webhook_event(self, event_id: str, repo_id: str, event_type: str, payload: str | None = None) -> None:
        """Record a received webhook event."""
        with self._lock:
            self._conn.execute(
                """INSERT OR IGNORE INTO webhook_events
                   (event_id, repo_id, event_type, payload, received_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (event_id, repo_id, event_type, payload, _now()),
            )
            self._conn.commit()

    def is_webhook_event_processed(self, event_id: str) -> bool:
        """Check if a webhook event has been processed."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT processed FROM webhook_events WHERE event_id = ?",
                (event_id,),
            )
            row = cur.fetchone()
            return row is not None and row["processed"] == 1

    def mark_webhook_event_processed(self, event_id: str) -> None:
        """Mark a webhook event as processed."""
        with self._lock:
            self._conn.execute(
                "UPDATE webhook_events SET processed = 1 WHERE event_id = ?",
                (event_id,),
            )
            self._conn.commit()

    # --- GitHub App installations ---

    def add_installation(
        self,
        installation_id: str,
        account_login: str,
        account_type: str,
    ) -> None:
        """Record a GitHub App installation."""
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO installations
                   (installation_id, account_login, account_type, created_at)
                   VALUES (?, ?, ?, ?)""",
                (installation_id, account_login, account_type, _now()),
            )
            self._conn.commit()

    def get_installation(self, installation_id: str) -> sqlite3.Row | None:
        """Get a GitHub App installation record."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM installations WHERE installation_id = ?",
                (installation_id,),
            )
            return cur.fetchone()

    def delete_installation(self, installation_id: str) -> None:
        """Remove a GitHub App installation record."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM installations WHERE installation_id = ?",
                (installation_id,),
            )
            self._conn.commit()
