from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional


TERMINAL = {"completed", "failed", "cancelled"}


class PersistentScheduler:
    """Durable SQLite queue with atomic leases and bounded retry recovery."""

    def __init__(self, db_path: Path, runner: Callable[[Dict[str, Any]], Dict[str, Any]],
                 lease_s: int = 60, max_log_chars: int = 12000):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.runner, self.lease_s, self.max_log_chars = runner, max(1, lease_s), max(256, max_log_chars)
        self.worker_id = uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        with self._connect() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS scheduled_jobs(
              id TEXT PRIMARY KEY, command TEXT NOT NULL, cwd TEXT, env TEXT,
              run_at REAL NOT NULL, interval_s REAL, status TEXT NOT NULL,
              attempts INTEGER NOT NULL DEFAULT 0, max_retries INTEGER NOT NULL DEFAULT 0,
              lease_owner TEXT, lease_until REAL, cancel_requested INTEGER NOT NULL DEFAULT 0,
              output TEXT NOT NULL DEFAULT '', error TEXT, created_at REAL NOT NULL,
              updated_at REAL NOT NULL, completed_event_sent INTEGER NOT NULL DEFAULT 0,
              context_result TEXT, ledger_ref TEXT)
            """)
            db.execute("UPDATE scheduled_jobs SET status='queued', lease_owner=NULL, lease_until=NULL "
                       "WHERE status='running' AND (lease_until IS NULL OR lease_until < ?)", (time.time(),))

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        return db

    def schedule(self, command: str, run_at: Optional[float] = None, interval_s: Optional[float] = None,
                 cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None,
                 max_retries: int = 0, confirmed: bool = False) -> Dict[str, Any]:
        if not command.strip(): raise ValueError("command is required")
        if interval_s is not None and interval_s <= 0: raise ValueError("interval_s must be positive")
        if not confirmed:
            raise PermissionError("confirmation required for scheduled external work")
        now = time.time(); job_id = uuid.uuid4().hex[:16]
        with self._connect() as db:
            db.execute("INSERT INTO scheduled_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (job_id, command, cwd, json.dumps(env or {}), run_at or now, interval_s, "queued", 0,
               max(0, int(max_retries)), None, None, 0, '', None, now, now, 0, None, None))
        return self.get(job_id)

    def cancel(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as db:
            db.execute("UPDATE scheduled_jobs SET cancel_requested=1, status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END, updated_at=? WHERE id=?", (time.time(), job_id))
        return self.get(job_id)

    def get(self, job_id: str) -> Dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job_id,)).fetchone()
        if not row: raise KeyError(job_id)
        result = dict(row); result["env"] = json.loads(result["env"] or "{}")
        result.pop("command", None); result["job_id"] = result.pop("id")
        return result

    def list(self, status: Optional[str] = None):
        with self._connect() as db:
            rows = db.execute("SELECT id FROM scheduled_jobs WHERE (? IS NULL OR status=?) ORDER BY created_at DESC", (status, status)).fetchall()
        return [self.get(row[0]) for row in rows]

    def _lease(self):
        now = time.time()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM scheduled_jobs WHERE status='queued' AND cancel_requested=0 AND run_at<=? ORDER BY run_at LIMIT 1", (now,)).fetchone()
            if not row: db.execute("COMMIT"); return None
            db.execute("UPDATE scheduled_jobs SET status='running', lease_owner=?, lease_until=?, attempts=attempts+1, updated_at=? WHERE id=? AND status='queued'", (self.worker_id, now+self.lease_s, now, row["id"]))
            db.execute("COMMIT"); return dict(row)

    def _finish(self, job: Dict[str, Any], result: Dict[str, Any], ok: bool):
        now = time.time(); output = json.dumps(result, ensure_ascii=False, default=str)[:self.max_log_chars]
        with self._connect() as db:
            row = db.execute("SELECT * FROM scheduled_jobs WHERE id=?", (job["id"],)).fetchone()
            if not row: return
            retry = not ok and row["attempts"] <= row["max_retries"] and not row["cancel_requested"]
            status = "queued" if retry else ("completed" if ok else ("cancelled" if row["cancel_requested"] else "failed"))
            next_run = now + (row["interval_s"] or 0) if ok and row["interval_s"] else now
            if ok and row["interval_s"]: status = "queued"
            db.execute("UPDATE scheduled_jobs SET status=?, run_at=?, lease_owner=NULL, lease_until=NULL, output=?, error=?, updated_at=?, completed_event_sent=CASE WHEN ? THEN 1 ELSE completed_event_sent END WHERE id=? AND lease_owner=?", (status, next_run, output, None if ok else str(result)[:self.max_log_chars], now, int(ok and not row["interval_s"]), job["id"], self.worker_id))

    def _run_once(self):
        job = self._lease()
        if not job: return False
        try: result = self.runner(job); self._finish(job, result, True)
        except Exception as exc: self._finish(job, {"error": str(exc)}, False)
        return True

    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread = threading.Thread(target=self._loop, daemon=True); self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            self._run_once() or self._stop.wait(.25)

    def stop(self): self._stop.set(); self._thread.join(2) if self._thread else None
