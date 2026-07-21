import tempfile
import time
import unittest
from pathlib import Path

from mcp_server.persistent_scheduler import PersistentScheduler


class PersistentSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def test_confirmation_and_exactly_once_after_restart(self):
        def runner(job):
            self.seen.append(job["id"])
            return {"ok": True, "output": "done"}
        db = Path(self.tmp.name) / "queue.sqlite3"
        first = PersistentScheduler(db, runner)
        with self.assertRaises(PermissionError): first.schedule("echo x")
        job = first.schedule("echo x", confirmed=True)
        self.assertTrue(first._run_once())
        second = PersistentScheduler(db, runner)
        self.assertEqual(second.get(job["job_id"])["status"], "completed")
        self.assertEqual(self.seen.count(job["job_id"]), 1)

    def test_retry_is_bounded_and_cancellation_is_durable(self):
        attempts = []
        def runner(job):
            attempts.append(1)
            raise RuntimeError("no")
        scheduler = PersistentScheduler(Path(self.tmp.name) / "q.db", runner)
        job = scheduler.schedule("false", max_retries=2, confirmed=True)
        for _ in range(3): scheduler._run_once()
        self.assertEqual(len(attempts), 3)
        self.assertEqual(scheduler.get(job["job_id"])["status"], "failed")
        cancelled = scheduler.schedule("sleep 10", confirmed=True)
        self.assertEqual(scheduler.cancel(cancelled["job_id"])["status"], "cancelled")

    def test_interval_reschedules(self):
        scheduler = PersistentScheduler(Path(self.tmp.name) / "q.db", lambda job: {"ok": True})
        job = scheduler.schedule("echo x", interval_s=60, confirmed=True)
        scheduler._run_once()
        self.assertEqual(scheduler.get(job["job_id"])["status"], "queued")


if __name__ == "__main__":
    unittest.main()
