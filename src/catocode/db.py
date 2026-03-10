"""Database connection abstraction supporting SQLite and PostgreSQL.

Usage:
    conn = connect()          # Auto-detect from CATOCODE_DATABASE_URL
    conn = connect(url)       # Explicit URL

    conn.execute(sql, params) -> rows
    conn.executemany(sql, params_list)
    conn.executescript(sql)   # DDL / multi-statement
    conn.commit()
    conn.close()

Both backends normalise:
  - Placeholder syntax  (? vs %s)
  - Row access          (sqlite3.Row-like dict-of-columns for both)
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _sqlite_placeholder(sql: str) -> str:
    return sql  # SQLite already uses ?


def _pg_placeholder(sql: str) -> str:
    """Convert SQLite-style ? placeholders to PostgreSQL %s."""
    result, i = [], 0
    while i < len(sql):
        if sql[i] == "?" and (i == 0 or sql[i - 1] != "'"):
            result.append("%s")
        else:
            result.append(sql[i])
        i += 1
    return "".join(result)


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------

class _SQLiteConn:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()

    def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            rows = cur.fetchall()
            return [dict(r) for r in rows]

    def execute_one(self, sql: str, params: tuple = ()) -> dict | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, params_list)

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)
            self._conn.commit()

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @property
    def backend(self) -> str:
        return "sqlite"


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------

class _PGConn:
    def __init__(self, dsn: str) -> None:
        import psycopg2
        import psycopg2.extras

        self._conn = psycopg2.connect(dsn)
        self._conn.autocommit = False
        self._lock = threading.Lock()
        self._DictCursor = psycopg2.extras.RealDictCursor

    def _cur(self):
        return self._conn.cursor(cursor_factory=self._DictCursor)

    def execute(self, sql: str, params: tuple = ()) -> list[dict]:
        sql = _pg_placeholder(sql)
        # Rewrite SQLite-specific syntax for PostgreSQL
        sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO").replace(
            "INSERT OR REPLACE INTO", "INSERT INTO"
        )
        with self._lock:
            cur = self._cur()
            try:
                cur.execute(sql, params)
                try:
                    rows = cur.fetchall()
                    return [dict(r) for r in rows]
                except Exception:
                    return []
            finally:
                cur.close()

    def execute_one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self.execute(sql, params)
        return rows[0] if rows else None

    def executemany(self, sql: str, params_list: list[tuple]) -> None:
        sql = _pg_placeholder(sql)
        with self._lock:
            cur = self._cur()
            try:
                cur.executemany(sql, params_list)
            finally:
                cur.close()

    def executescript(self, sql: str) -> None:
        """Execute DDL statements (CREATE TABLE, etc.)."""
        # Split on ; and run each non-empty statement
        with self._lock:
            cur = self._cur()
            try:
                for stmt in sql.split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        # PostgreSQL uses SERIAL instead of INTEGER PRIMARY KEY AUTOINCREMENT
                        stmt = stmt.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
                        # INSERT OR IGNORE / OR REPLACE → handled at execute time
                        cur.execute(stmt)
            finally:
                cur.close()
            self._conn.commit()

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @property
    def backend(self) -> str:
        return "postgresql"


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

Connection = _SQLiteConn | _PGConn


def connect(url: str | None = None) -> Connection:
    """Create and return a database connection.

    Args:
        url: Connection URL.  Supported formats:
             - ``sqlite:///path/to/db``   (or just a filesystem path)
             - ``postgresql://user:pass@host/db``

             If None, reads ``CATOCODE_DATABASE_URL`` env var, then falls
             back to ``~/.catocode/catocode.db``.
    """
    if url is None:
        url = os.environ.get("CATOCODE_DATABASE_URL") or os.environ.get("DATABASE_URL", "")

    if url.startswith("postgresql://") or url.startswith("postgres://"):
        return _PGConn(url)

    # Treat as a filesystem path (with optional sqlite:/// prefix)
    path_str = url.removeprefix("sqlite:///").strip() if url else ""
    if not path_str:
        path_str = os.environ.get("CATOCODE_DB_PATH", "")
    if not path_str:
        path_str = str(Path.home() / ".catocode" / "catocode.db")
    return _SQLiteConn(Path(path_str))
