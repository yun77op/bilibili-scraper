"""SQLite data layer — shared state between server and worker processes.

Replaces the in-memory ``jobs`` dict / ``job_queue`` list from the original
monolithic app so that the HTTP server and the background worker can run as
independent processes while sharing job state.

Concurrency: SQLite in WAL mode + ``busy_timeout`` is safe for one writer
(worker) and multiple readers (server threads) on a local filesystem.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent / "jobs.db"


def _connect() -> sqlite3.Connection:
    """Return a new connection with WAL mode and a generous busy timeout."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    url             TEXT    NOT NULL,
    title           TEXT    DEFAULT '',
    cookie_string   TEXT    DEFAULT '',
    status          TEXT    DEFAULT 'queued',
    stage           TEXT    DEFAULT '等待开始',
    logs            TEXT    DEFAULT '[]',
    progress        INTEGER DEFAULT 0,
    transcript      TEXT    DEFAULT '',
    article         TEXT    DEFAULT '',
    error           TEXT    DEFAULT '',
    output_dir      TEXT    DEFAULT '',
    page_output_dirs TEXT   DEFAULT '[]',
    page_articles   TEXT    DEFAULT '[]',
    created_at      REAL,
    updated_at      REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_status  ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
"""


def init_db() -> None:
    """Ensure the schema exists.  Safe to call multiple times / from every process."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        # Migration: add title column for existing databases
        try:
            conn.execute("ALTER TABLE jobs ADD COLUMN title TEXT DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict, decoding JSON columns."""
    d = dict(row)
    for key in ("logs", "page_output_dirs", "page_articles"):
        try:
            d[key] = json.loads(d[key])
        except (json.JSONDecodeError, KeyError):
            d[key] = []
    return d


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_job(
    *,
    job_id: str,
    url: str,
    title: str = "",
    cookie_string: str = "",
) -> dict[str, Any]:
    """Insert a new job and return it as a dict."""
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO jobs (id, url, title, cookie_string, status, stage, logs, progress,
               transcript, article, error, output_dir, page_output_dirs, page_articles,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', '排队等待', '["已加入任务队列"]', 0,
                       '', '', '', '', '[]', '[]', ?, ?)""",
            (job_id, url, title, cookie_string, now, now),
        )
        conn.commit()
        return _row_to_dict(
            conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        )
    finally:
        conn.close()


def get_job(job_id: str) -> dict[str, Any] | None:
    """Return full job data or *None*."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_job_snapshot(job_id: str) -> dict[str, Any] | None:
    """Lightweight status check used by the server polling endpoint."""
    job = get_job(job_id)
    if job is None:
        return None
    elapsed = 0
    if job["status"] in ("running", "queued"):
        elapsed = int(time.time() - job["created_at"])
    job["elapsed"] = elapsed
    return job


def claim_next_queued_job() -> dict[str, Any] | None:
    """Atomically pick the oldest queued job and mark it *running*.

    Returns the job dict so the worker can reconstruct a ``Job`` object.
    Returns *None* when the queue is empty.
    """
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT id FROM jobs
               WHERE status = 'queued'
               ORDER BY created_at ASC
               LIMIT 1"""
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        job_id = row["id"]
        now = time.time()
        conn.execute(
            """UPDATE jobs
               SET status = 'running', stage = '任务已开始', progress = 5, updated_at = ?
               WHERE id = ?""",
            (now, job_id),
        )
        conn.commit()
        return _row_to_dict(
            conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_job_log(job_id: str, message: str, progress: int | None = None) -> None:
    """Append a log line and update *stage* / *progress*."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT logs, progress, status FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is None:
            return
        logs: list[str] = json.loads(row["logs"])
        logs.append(message)
        new_progress = progress if progress is not None else row["progress"]
        conn.execute(
            """UPDATE jobs
               SET stage = ?, logs = ?, progress = ?, updated_at = ?
               WHERE id = ?""",
            (message, json.dumps(logs, ensure_ascii=False), new_progress, time.time(), job_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_job(
    job_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    progress: int | None = None,
    title: str | None = None,
    transcript: str | None = None,
    article: str | None = None,
    error: str | None = None,
    output_dir: str | None = None,
    page_output_dirs: list[str] | None = None,
    page_articles: list[str] | None = None,
) -> None:
    """Update one or more fields of a job.  Only supplied kwargs are written."""
    conn = _connect()
    try:
        sets: list[str] = ["updated_at = ?"]
        params: list[Any] = [time.time()]

        for col, val in (
            ("status", status),
            ("stage", stage),
            ("progress", progress),
            ("title", title),
            ("transcript", transcript),
            ("article", article),
            ("error", error),
            ("output_dir", output_dir),
            ("page_output_dirs", json.dumps(page_output_dirs, ensure_ascii=False) if page_output_dirs is not None else None),
            ("page_articles", json.dumps(page_articles, ensure_ascii=False) if page_articles is not None else None),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)

        params.append(job_id)
        conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def job_exists(job_id: str) -> bool:
    """Check if a job id is known."""
    conn = _connect()
    try:
        row = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def get_worker_heartbeat() -> float | None:
    """Return the last time (epoch) the worker wrote a heartbeat, or *None*."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT updated_at FROM jobs WHERE id = '__worker_heartbeat__'"
        ).fetchone()
        return row["updated_at"] if row else None
    finally:
        conn.close()


def set_worker_heartbeat() -> None:
    """Write a heartbeat so the server / UI can tell the worker is alive."""
    conn = _connect()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO jobs
               (id, url, status, stage, created_at, updated_at)
               VALUES ('__worker_heartbeat__', '', 'heartbeat', '', ?, ?)""",
            (time.time(), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def cleanup_stale_jobs() -> int:
    """Reset any stale 'running' jobs (left by a killed worker) back to 'queued'.

    Returns the number of jobs that were reset.
    """
    conn = _connect()
    try:
        cursor = conn.execute(
            """UPDATE jobs
               SET status = 'queued', stage = '排队等待（重试）', error = ''
               WHERE status = 'running' AND id != '__worker_heartbeat__'"""
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def list_all_jobs(limit: int = 50) -> list[dict[str, Any]]:
    """Return jobs (excluding heartbeat): active jobs always first, then recent ones, newest first."""
    conn = _connect()
    try:
        active = conn.execute(
            """SELECT * FROM jobs
               WHERE id != '__worker_heartbeat__' AND status IN ('running', 'queued')
               ORDER BY created_at"""
        ).fetchall()
        recent = conn.execute(
            """SELECT * FROM jobs
               WHERE id != '__worker_heartbeat__' AND status NOT IN ('running', 'queued')
               ORDER BY created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in active] + [_row_to_dict(r) for r in recent]
    finally:
        conn.close()


def cancel_job(job_id: str) -> bool:
    """Cancel a queued or running job. Returns True if a row was affected."""
    conn = _connect()
    try:
        cursor = conn.execute(
            """UPDATE jobs
               SET status = 'cancelled', stage = '已取消', error = '用户取消'
               WHERE id = ? AND status IN ('queued', 'running')""",
            (job_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def is_job_cancelled(job_id: str) -> bool:
    """Check whether a job has been cancelled."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE id = ? AND status = 'cancelled'",
            (job_id,),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def retry_job(job_id: str) -> bool:
    """Reset a failed or cancelled job back to queued so the worker picks it up again.

    Returns True if a row was affected.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT logs FROM jobs WHERE id = ? AND status IN ('error', 'cancelled')",
            (job_id,),
        ).fetchone()
        if row is None:
            return False
        logs: list[str] = json.loads(row["logs"])
        logs.append("用户点击重试")
        now = time.time()
        cursor = conn.execute(
            """UPDATE jobs
               SET status = 'queued', stage = '排队等待（重试）', error = '',
                   transcript = '', article = '', title = '', progress = 0,
                   output_dir = '', page_output_dirs = '[]', page_articles = '[]',
                   logs = ?, updated_at = ?
               WHERE id = ? AND status IN ('error', 'cancelled')""",
            (json.dumps(logs, ensure_ascii=False), now, job_id),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_job(job_id: str) -> bool:
    """Delete a job. Returns True if a row was affected."""
    conn = _connect()
    try:
        cursor = conn.execute(
            "DELETE FROM jobs WHERE id = ? AND id != '__worker_heartbeat__'",
            (job_id,),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()
