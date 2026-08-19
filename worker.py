"""Independent worker process — polls SQLite for queued jobs and executes them.

Usage::

    python worker.py [--interval 2]

The worker does **not** need the HTTP server to be running — it reads from and
writes to the same SQLite database that the server uses.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import traceback

# Make sure the project root is on the path so we can import app.
ROOT = __import__("pathlib").Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app import Job, load_local_env, process_job  # noqa: E402
from db import (  # noqa: E402
    claim_next_queued_job,
    cleanup_stale_jobs,
    init_db,
    is_job_cancelled,
    set_worker_heartbeat,
    update_job,
    update_job_log,
)

load_local_env()

shutdown_requested = False
_current_job_id: str | None = None
_heartbeat_stop: threading.Event | None = None
_heartbeat_thread: threading.Thread | None = None


def _heartbeat_loop(stop_event: threading.Event, interval: float = 5.0) -> None:
    """Daemon thread that writes a heartbeat every *interval* seconds."""
    while not stop_event.is_set():
        try:
            set_worker_heartbeat()
        except Exception:
            pass
        stop_event.wait(interval)


def _start_heartbeat() -> None:
    """Start the heartbeat daemon thread."""
    global _heartbeat_stop, _heartbeat_thread
    _heartbeat_stop = threading.Event()
    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(_heartbeat_stop,), daemon=True)
    _heartbeat_thread.start()


def _stop_heartbeat() -> None:
    """Stop the heartbeat daemon thread."""
    global _heartbeat_stop, _heartbeat_thread
    if _heartbeat_stop:
        _heartbeat_stop.set()
    if _heartbeat_thread:
        _heartbeat_thread.join(timeout=2)
    _heartbeat_stop = None
    _heartbeat_thread = None


def _handle_signal(signum: int, frame: object) -> None:
    global shutdown_requested
    shutdown_requested = True
    if _current_job_id:
        update_job_log(
            _current_job_id,
            f"Worker 收到信号 {signum}，将在当前任务完成后退出",
        )
    print(f"\n[worker] 收到信号 {signum}，正在退出...", file=sys.stderr)


def _build_job(data: dict) -> Job:
    """Reconstruct a ``Job`` object from a DB row dict."""
    return Job(
        id=data["id"],
        url=data["url"],
        title=data.get("title", ""),
        cookie_string=data.get("cookie_string", ""),
        user_id=data.get("user_id", ""),
        status=data["status"],
        stage=data["stage"],
        logs=data.get("logs", []) or [],
        progress=data.get("progress", 0),
        transcript=data.get("transcript", ""),
        article=data.get("article", ""),
        error=data.get("error", ""),
        output_dir=data.get("output_dir", ""),
        page_output_dirs=data.get("page_output_dirs", []) or [],
        page_articles=data.get("page_articles", []) or [],
        created_at=data.get("created_at", time.time()),
        updated_at=data.get("updated_at", time.time()),
    )


def _patch_job_for_persistence(job: Job) -> None:
    """Make ``job.log()`` also persist to the SQLite database and keep the
    heartbeat alive so the web UI doesn't think the worker is offline during
    long-running tasks."""
    original_log = job.log

    def _persist_and_log(message: str, progress: int | None = None) -> None:
        original_log(message, progress)
        try:
            set_worker_heartbeat()
            update_job_log(job.id, message, progress)
        except Exception:
            # Don't crash the worker if DB write fails sporadically
            print(f"[worker] DB write failed for log: {message}", file=sys.stderr)

    job.log = _persist_and_log  # type: ignore[method-assign]


def run_loop(poll_interval: float = 2.0) -> None:
    """Main worker loop: claim → process → repeat."""
    global _current_job_id

    stale_count = cleanup_stale_jobs()
    if stale_count:
        print(f"[worker] 清理了 {stale_count} 个僵尸任务", file=sys.stderr)

    print(f"[worker] 已启动 pid={os.getpid()}，等待任务...", file=sys.stderr)
    set_worker_heartbeat()

    while not shutdown_requested:
        try:
            cleanup_stale_jobs()
            job_data = claim_next_queued_job()
        except Exception as exc:
            print(f"[worker] 查询队列失败: {exc}", file=sys.stderr)
            time.sleep(poll_interval)
            continue

        if job_data is None:
            set_worker_heartbeat()
            time.sleep(poll_interval)
            continue

        _current_job_id = job_data["id"]
        job = _build_job(job_data)
        _patch_job_for_persistence(job)

        print(f"[worker] 开始处理: {job.id}  {job.url}", file=sys.stderr)
        _start_heartbeat()
        try:
            process_job(job)
        except Exception as exc:
            # process_job already catches inside — this is a last-resort guard
            tb = traceback.format_exc()
            print(f"[worker] 未捕获异常: {exc}\n{tb}", file=sys.stderr)
            try:
                update_job(job.id, status="error", error=str(exc))
            except Exception:
                pass
        finally:
            _stop_heartbeat()

        # 如果任务已被用户取消，不要覆盖取消状态
        if is_job_cancelled(job.id):
            print(f"[worker] 任务 {job.id} 已被用户取消，跳过状态更新", file=sys.stderr)
            _current_job_id = None
            continue

        # Persist final fields that process_job sets on the Job object
        try:
            update_job(
                job.id,
                status=job.status,
                stage=job.stage,
                progress=job.progress,
                title=job.title,
                transcript=job.transcript,
                article=job.article,
                error=job.error,
                output_dir=job.output_dir,
                page_output_dirs=job.page_output_dirs or None,
                page_articles=job.page_articles or None,
            )
        except Exception:
            pass

        _current_job_id = None

    print("[worker] 已停止", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bilibili/YouTube 后台任务 Worker")
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="队列轮询间隔（秒），默认 2",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    init_db()
    run_loop(poll_interval=args.interval)


if __name__ == "__main__":
    main()
