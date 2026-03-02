from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    id TEXT PRIMARY KEY,
    repo_url TEXT NOT NULL,
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
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        if db_path is None:
            db_path = Path.home() / ".repocraft" / "repocraft.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

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

    # --- logs ---

    def add_log(self, activity_id: str, line: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs (activity_id, line, ts) VALUES (?, ?, ?)",
                (activity_id, line, _now()),
            )
            self._conn.commit()

    def get_logs(self, activity_id: str) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM logs WHERE activity_id = ? ORDER BY id",
                (activity_id,),
            )
            return cur.fetchall()
