"""多 worker 下僵尸任务回收：只重排已死 PID，不抢仍在跑的任务。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db


class StaleJobCleanupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        db.init_db()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def _insert_running(self, job_id: str, pid: int) -> None:
        conn = db._connect()
        try:
            now = 1.0
            conn.execute(
                """INSERT INTO jobs (id, url, status, stage, worker_pid, created_at, updated_at)
                   VALUES (?, 'https://example.com', 'running', '任务已开始', ?, ?, ?)""",
                (job_id, pid, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def test_live_pid_not_requeued(self):
        self._insert_running("live", os.getpid())
        self.assertEqual(db.cleanup_stale_jobs(), 0)
        job = db.get_job("live")
        self.assertEqual(job["status"], "running")

    def test_dead_pid_requeued(self):
        self._insert_running("dead", 999999)
        self.assertEqual(db.cleanup_stale_jobs(), 1)
        job = db.get_job("dead")
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["worker_pid"], 0)

    def test_missing_pid_requeued(self):
        self._insert_running("orphan", 0)
        self.assertEqual(db.cleanup_stale_jobs(), 1)
        self.assertEqual(db.get_job("orphan")["status"], "queued")

    def test_claim_records_current_pid(self):
        db.create_job(job_id="n1", url="https://bilibili.com/video/BV1", user_id="u")
        claimed = db.claim_next_queued_job()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["worker_pid"], os.getpid())
        self.assertEqual(db.cleanup_stale_jobs(), 0)


if __name__ == "__main__":
    unittest.main()
