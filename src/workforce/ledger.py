"""SQLite task ledger: one row per delegated task, plus an event log.

Worker processes and the MCP server share this file, so every call opens a
short-lived connection (WAL mode, busy timeout) instead of holding one open.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# queued -> running -> done | failed ; active -> cancelled ; done -> merged.
# Removing the worktree sets `cleaned_at` and keeps the status (history stays intact).
STATUSES = ("queued", "running", "done", "failed", "cancelled", "merged")
ACTIVE = ("queued", "running")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    role          TEXT NOT NULL,
    engine        TEXT NOT NULL,
    status        TEXT NOT NULL,
    title         TEXT NOT NULL,
    instructions  TEXT NOT NULL,
    acceptance    TEXT NOT NULL DEFAULT '[]',
    context_files TEXT NOT NULL DEFAULT '[]',
    project_path  TEXT,
    base_ref      TEXT,
    base_branch   TEXT,
    branch        TEXT,
    worktree      TEXT,
    write         INTEGER NOT NULL DEFAULT 0,
    local_only    INTEGER NOT NULL DEFAULT 0,
    revision      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    model_used    TEXT,
    pid           INTEGER,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    seconds       REAL,
    result        TEXT,
    error         TEXT,
    commit_sha    TEXT,
    verification  TEXT,
    merged_at     REAL,
    cleaned_at    REAL,
    tier          TEXT,
    tier_reason   TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  TEXT NOT NULL,
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_task ON events(task_id);
CREATE TABLE IF NOT EXISTS feedback (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id  TEXT NOT NULL,
    revision INTEGER NOT NULL,
    ts       REAL NOT NULL,
    text     TEXT NOT NULL
);
"""

_JSON_FIELDS = ("acceptance", "context_files", "verification")
_BOOL_FIELDS = ("write", "local_only")


@dataclass
class Task:
    id: str
    role: str
    engine: str
    status: str
    title: str
    instructions: str
    acceptance: list[str] = field(default_factory=list)
    context_files: list[str] = field(default_factory=list)
    project_path: str | None = None
    base_ref: str | None = None
    base_branch: str | None = None
    branch: str | None = None
    worktree: str | None = None
    write: bool = False
    local_only: bool = False
    revision: int = 0
    attempts: int = 0
    model_used: str | None = None
    pid: int | None = None
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    seconds: float | None = None
    result: str | None = None
    error: str | None = None
    commit_sha: str | None = None
    verification: dict | None = None
    merged_at: float | None = None
    cleaned_at: float | None = None
    tier: str | None = None          # light | standard | heavy
    tier_reason: str | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Task:
        data = dict(row)
        for k in _JSON_FIELDS:
            data[k] = json.loads(data[k]) if data.get(k) else ([] if k != "verification" else None)
        for k in _BOOL_FIELDS:
            data[k] = bool(data[k])
        return cls(**data)


def new_task_id() -> str:
    return time.strftime("t%Y%m%d-%H%M%S-") + secrets.token_hex(2)


class Ledger:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)
            # Add columns introduced after a ledger file was first created.
            have = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
            for col, kind in (("cleaned_at", "REAL"), ("tier", "TEXT"),
                              ("tier_reason", "TEXT")):
                if col not in have:
                    c.execute(f"ALTER TABLE tasks ADD COLUMN {col} {kind}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # ---------- tasks ----------

    def create(self, task: Task) -> Task:
        task.created_at = task.created_at or time.time()
        data = _encode(vars(task))
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self._conn() as c:
            c.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(data.values()))
        self.event(task.id, "created", f"{task.role} via {task.engine}")
        return task

    def get(self, task_id: str) -> Task:
        with self._conn() as c:
            row = c.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"no task '{task_id}'")
        return Task.from_row(row)

    def update(self, task_id: str, *, only_if_status: tuple[str, ...] | None = None,
               **fields: Any) -> bool:
        """Update fields. With `only_if_status`, change nothing unless the task
        is currently in one of those statuses (returns False then)."""
        if not fields:
            return False
        data = _encode(fields)
        sets = ", ".join(f"{k} = ?" for k in data)
        sql = f"UPDATE tasks SET {sets} WHERE id = ?"
        params = [*data.values(), task_id]
        if only_if_status:
            sql += f" AND status IN ({', '.join('?' for _ in only_if_status)})"
            params += list(only_if_status)
        with self._conn() as c:
            return c.execute(sql, params).rowcount > 0

    def list(self, status: str | None = None, limit: int = 50) -> list[Task]:
        sql, params = "SELECT * FROM tasks", []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as c:
            return [Task.from_row(r) for r in c.execute(sql, params)]

    def claim_slot(self, task_id: str, pid: int, max_concurrent: int,
                   is_alive=None) -> str:
        """Atomically move a queued task to running if a slot is free.

        Returns "claimed", "wait" (no free slot) or "gone" (not queued any more).
        Running tasks whose process died are marked failed first.
        """
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                status = c.execute("SELECT status FROM tasks WHERE id = ?",
                                   (task_id,)).fetchone()
                if status is None or status[0] != "queued":
                    c.execute("COMMIT")
                    return "gone"
                running = c.execute(
                    "SELECT id, pid FROM tasks WHERE status = 'running'").fetchall()
                alive = 0
                for r in running:
                    if is_alive is None or (r["pid"] and is_alive(r["pid"])):
                        alive += 1
                    else:
                        c.execute("UPDATE tasks SET status='failed', finished_at=?, "
                                  "error='worker process died' WHERE id=?",
                                  (time.time(), r["id"]))
                if alive >= max_concurrent:
                    c.execute("COMMIT")
                    return "wait"
                c.execute("UPDATE tasks SET status='running', pid=?, started_at=?, "
                          "attempts=attempts+1, error=NULL WHERE id=?",
                          (pid, time.time(), task_id))
                c.execute("COMMIT")
                return "claimed"
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # ---------- events & feedback ----------

    def event(self, task_id: str, kind: str, detail: str = "") -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events (task_id, ts, kind, detail) VALUES (?, ?, ?, ?)",
                      (task_id, time.time(), kind, detail[:4000]))

    def events(self, task_id: str, limit: int = 30) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT ts, kind, detail FROM events WHERE task_id = ? "
                             "ORDER BY id DESC LIMIT ?", (task_id, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def add_feedback(self, task_id: str, revision: int, text: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO feedback (task_id, revision, ts, text) VALUES (?, ?, ?, ?)",
                      (task_id, revision, time.time(), text))

    def feedback(self, task_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT revision, ts, text FROM feedback WHERE task_id = ? "
                             "ORDER BY id", (task_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- reporting ----------

    def stats(self) -> list[dict]:
        """Per role/engine/model: counts, success rate, average seconds."""
        sql = """
            SELECT role, engine, COALESCE(tier, '-') AS tier,
                   COALESCE(model_used, '-') AS model,
                   COUNT(*) AS tasks,
                   SUM(status IN ('done', 'merged')) AS succeeded,
                   SUM(status = 'failed') AS failed,
                   SUM(status = 'merged') AS merged,
                   ROUND(AVG(seconds), 1) AS avg_seconds,
                   SUM(revision) AS revisions
            FROM tasks GROUP BY role, engine, tier, model ORDER BY tasks DESC
        """
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql)]


def _encode(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k in _JSON_FIELDS:
            v = None if v is None else json.dumps(v)
        elif k in _BOOL_FIELDS:
            v = int(bool(v))
        out[k] = v
    return out
