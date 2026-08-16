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
    user_id         TEXT    DEFAULT '',
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

CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    username        TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    is_admin        INTEGER DEFAULT 0,
    is_active       INTEGER DEFAULT 1,
    settings        TEXT    DEFAULT '{}',
    failed_attempts INTEGER DEFAULT 0,
    locked_until    REAL    DEFAULT 0,
    created_at      REAL,
    last_login_at   REAL
);

CREATE TABLE IF NOT EXISTS gdrive_tokens (
    user_id     TEXT PRIMARY KEY,
    token_json  TEXT NOT NULL,
    updated_at  REAL
);
"""


def init_db() -> None:
    """Ensure the schema exists.  Safe to call multiple times / from every process."""
    conn = _connect()
    try:
        conn.executescript(_SCHEMA)
        # Migrations for existing databases
        for stmt in (
            "ALTER TABLE jobs ADD COLUMN title TEXT DEFAULT ''",
            "ALTER TABLE jobs ADD COLUMN user_id TEXT DEFAULT ''",
        ):
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists
        # Index on user_id (must run after the column migration above)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id)")
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
    user_id: str = "",
) -> dict[str, Any]:
    """Insert a new job and return it as a dict."""
    now = time.time()
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO jobs (id, user_id, url, title, cookie_string, status, stage, logs, progress,
               transcript, article, error, output_dir, page_output_dirs, page_articles,
               created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'queued', '排队等待', '["已加入任务队列"]', 0,
                       '', '', '', '', '[]', '[]', ?, ?)""",
            (job_id, user_id, url, title, cookie_string, now, now),
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


def list_user_jobs(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return a user's jobs: active jobs always first, then recent ones, newest first."""
    conn = _connect()
    try:
        active = conn.execute(
            """SELECT * FROM jobs
               WHERE user_id = ? AND status IN ('running', 'queued')
               ORDER BY created_at""",
            (user_id,),
        ).fetchall()
        recent = conn.execute(
            """SELECT * FROM jobs
               WHERE user_id = ? AND status NOT IN ('running', 'queued')
               ORDER BY created_at DESC
               LIMIT ?""",
            (user_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in active] + [_row_to_dict(r) for r in recent]
    finally:
        conn.close()


def list_user_jobs_page(user_id: str, page: int = 1, per_page: int = 20) -> dict:
    """Return one page of a user's jobs (active jobs first, then newest) plus pagination info."""
    total = count_user_jobs(user_id)
    if per_page <= 0:
        per_page = 20
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT * FROM jobs
               WHERE user_id = ? AND id != '__worker_heartbeat__'
               ORDER BY CASE WHEN status IN ('running', 'queued') THEN 0 ELSE 1 END,
                        created_at DESC
               LIMIT ? OFFSET ?""",
            (user_id, per_page, (page - 1) * per_page),
        ).fetchall()
        active = conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE user_id = ? AND id != '__worker_heartbeat__'
                 AND status IN ('running', 'queued')""",
            (user_id,),
        ).fetchone()[0]
        return {
            "jobs": [_row_to_dict(r) for r in rows],
            "total": total,
            "active": active,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }
    finally:
        conn.close()


def count_user_jobs(user_id: str) -> int:
    """Total number of a user's jobs (excluding heartbeat)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE user_id = ? AND id != '__worker_heartbeat__'",
            (user_id,),
        ).fetchone()
        return row[0]
    finally:
        conn.close()


def get_user_job(user_id: str, job_id: str) -> dict[str, Any] | None:
    """Return a job only if it belongs to the given user (ownership check)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
            (job_id, user_id),
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def get_user_job_snapshot(user_id: str, job_id: str) -> dict[str, Any] | None:
    """Ownership-checked snapshot for the server polling endpoint."""
    job = get_user_job(user_id, job_id)
    if job is None:
        return None
    elapsed = 0
    if job["status"] in ("running", "queued"):
        elapsed = int(time.time() - job["created_at"])
    job["elapsed"] = elapsed
    return job


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def _user_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["settings"] = json.loads(d["settings"]) if d["settings"] else {}
    except (json.JSONDecodeError, KeyError):
        d["settings"] = {}
    d["is_admin"] = bool(d["is_admin"])
    d["is_active"] = bool(d["is_active"])
    return d


def create_user(
    *,
    user_id: str,
    username: str,
    password_hash: str,
) -> dict[str, Any] | None:
    """Insert a new user.  Returns the user dict, or None if the username is taken."""
    now = time.time()
    conn = _connect()
    try:
        try:
            conn.execute(
                """INSERT INTO users (id, username, password_hash, is_admin, is_active,
                   settings, created_at)
                   VALUES (?, ?, ?, 0, 1, '{}', ?)""",
                (user_id, username, password_hash, now),
            )
        except sqlite3.IntegrityError:
            return None
        # Claim any legacy jobs (created before user accounts) for this user
        conn.execute(
            "UPDATE jobs SET user_id = ? WHERE user_id = '' AND id != '__worker_heartbeat__'",
            (user_id,),
        )
        conn.commit()
        return get_user(user_id)
    finally:
        conn.close()


def get_user(user_id: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user_row_to_dict(row) if row else None
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return _user_row_to_dict(row) if row else None
    finally:
        conn.close()


def list_users() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [_user_row_to_dict(r) for r in rows]
    finally:
        conn.close()


def count_users() -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])
    finally:
        conn.close()


def update_user(
    user_id: str,
    *,
    password_hash: str | None = None,
    is_admin: bool | None = None,
    is_active: bool | None = None,
    settings: dict[str, Any] | None = None,
    last_login_at: float | None = None,
    failed_attempts: int | None = None,
    locked_until: float | None = None,
) -> None:
    """Update one or more fields of a user."""
    conn = _connect()
    try:
        sets: list[str] = []
        params: list[Any] = []
        for col, val in (
            ("password_hash", password_hash),
            ("is_admin", int(is_admin) if is_admin is not None else None),
            ("is_active", int(is_active) if is_active is not None else None),
            ("settings", json.dumps(settings, ensure_ascii=False) if settings is not None else None),
            ("last_login_at", last_login_at),
            ("failed_attempts", failed_attempts),
            ("locked_until", locked_until),
        ):
            if val is not None:
                sets.append(f"{col} = ?")
                params.append(val)
        if not sets:
            return
        params.append(user_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
    finally:
        conn.close()


def set_user_login_attempt(user_id: str, failed_attempts: int, locked_until: float) -> None:
    """Record a failed login attempt / lockout."""
    update_user(
        user_id,
        failed_attempts=failed_attempts,
        locked_until=locked_until,
    )


def reset_login_attempts(user_id: str) -> None:
    update_user(user_id, failed_attempts=0, locked_until=0)


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


# ---------------------------------------------------------------------------
# Google Drive tokens (per-user, stored in DB)
# ---------------------------------------------------------------------------

def get_gdrive_token(user_id: str) -> str | None:
    """Return the stored Google Drive token JSON for a user, or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT token_json FROM gdrive_tokens WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return row["token_json"] if row else None
    finally:
        conn.close()


def save_gdrive_token(user_id: str, token_json: str) -> None:
    """Insert or replace the Google Drive token JSON for a user."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO gdrive_tokens (user_id, token_json, updated_at) "
            "VALUES (?, ?, ?)",
            (user_id, token_json, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_gdrive_token(user_id: str) -> None:
    """Delete the stored Google Drive token for a user (e.g. revoke)."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM gdrive_tokens WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
