from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from myatri.contracts import JobSpec, uid


class LostLease(RuntimeError):
    pass


class JobQueue:
    """Separate runtime database, intentionally not rebuilt from memory archives."""
    def __init__(self, path: Path, agent_id="atri", user_id="local"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.agent_id, self.user_id = agent_id, user_id
        with self.db() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, agent TEXT NOT NULL, user TEXT NOT NULL, key TEXT NOT NULL,
              role TEXT NOT NULL, payload TEXT NOT NULL, priority INTEGER NOT NULL,
              status TEXT NOT NULL, owner TEXT, lease_until REAL, attempts INTEGER DEFAULT 0,
              available REAL NOT NULL, result TEXT, error TEXT, UNIQUE(agent,user,key));
            CREATE TABLE IF NOT EXISTS schedule (agent TEXT, user TEXT, name TEXT, value TEXT,
              PRIMARY KEY(agent,user,name));
            CREATE TABLE IF NOT EXISTS usage (id INTEGER PRIMARY KEY, agent TEXT, user TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS protocols (agent TEXT,user TEXT,turn_id TEXT,payload TEXT,
              PRIMARY KEY(agent,user,turn_id));
            CREATE TABLE IF NOT EXISTS budget_reservations (
              id TEXT PRIMARY KEY, scope TEXT NOT NULL, day TEXT NOT NULL,
              reserved_tokens INTEGER NOT NULL, charged_tokens INTEGER,
              status TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS budget_scope_day ON budget_reservations(scope,day);
            CREATE INDEX IF NOT EXISTS budget_expiry ON budget_reservations(status,expires_at);
            CREATE TABLE IF NOT EXISTS budget_meta (name TEXT PRIMARY KEY, value INTEGER NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, spec: JobSpec, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,agent,user,key,role,payload,priority,status,available) VALUES (?,?,?,?,?,?,?,'pending',?)",
                       (uid("job"), self.agent_id, self.user_id, spec.idempotency_key, spec.role,
                        spec.model_dump_json(), spec.priority, now))
            return db.execute("SELECT id FROM jobs WHERE agent=? AND user=? AND key=?",
                              (self.agent_id, self.user_id, spec.idempotency_key)).fetchone()[0]

    def lease_job(self, owner, *, lease_seconds=120, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status=CASE WHEN result IS NULL THEN 'retry_wait' ELSE 'proposed' END,owner=NULL WHERE agent=? AND user=? AND status='running' AND lease_until<=?",
                       (self.agent_id, self.user_id, now))
            row = db.execute("SELECT * FROM jobs WHERE agent=? AND user=? AND status IN ('pending','retry_wait','deferred','proposed') AND available<=? AND (owner IS NULL OR lease_until<=?) ORDER BY priority DESC,available,id LIMIT 1",
                             (self.agent_id, self.user_id, now, now)).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                       (owner, now + lease_seconds, row["id"]))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def renew(self, job_id, owner, lease_seconds=120, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            changed = db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND agent=? AND user=? AND owner=? AND status='running' AND lease_until>?",
                                 (now + lease_seconds, job_id, self.agent_id, self.user_id, owner, now)).rowcount
            if not changed:
                raise LostLease(job_id)

    def save_result(self, job_id, owner, result, now=None):
        self._owned_update(job_id, owner, "result=?,status='running'", (json.dumps(result, ensure_ascii=False),), now)

    def finish(self, job_id, owner, status="succeeded", error=None, delay=0, now=None):
        if status not in {"succeeded", "committed", "deferred", "retry_wait", "failed", "cancelled"}:
            raise ValueError("invalid job status")
        now = time.time() if now is None else now
        self._owned_update(job_id, owner, "status=?,error=?,available=?,owner=NULL,lease_until=NULL",
                           (status, error, now + delay), now)

    def _owned_update(self, job_id, owner, assignments, params, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            changed = db.execute(f"UPDATE jobs SET {assignments} WHERE id=? AND agent=? AND user=? AND owner=? AND status='running' AND lease_until>?",
                                 (*params, job_id, self.agent_id, self.user_id, owner, now)).rowcount
            if not changed:
                raise LostLease(job_id)

    def list_jobs(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM jobs WHERE agent=? AND user=? ORDER BY rowid", (self.agent_id, self.user_id))]

    def retry_job(self, job_id, *, clear_result=False, now=None):
        """Explicit operator retry; a saved proposal is reused unless discarded."""
        return self._operator_update(job_id, "retry_wait", clear_result=clear_result, now=now)

    def retry_failed_once(self, job_id, recovery_version, *, now=None):
        """Atomically allow one upgrade recovery of the same immutable job.

        Attempts and saved results are retained. A subsequent failure is not
        made free to retry by another process or a restart.
        """
        now = time.time() if now is None else now
        name = "job_recovery:" + job_id + ":" + recovery_version
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM jobs WHERE id=? AND agent=? AND user=?",
                             (job_id, self.agent_id, self.user_id)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] != "failed" or db.execute(
                    "SELECT 1 FROM schedule WHERE agent=? AND user=? AND name=?",
                    (self.agent_id, self.user_id, name)).fetchone():
                return False
            db.execute("INSERT INTO schedule VALUES(?,?,?,?)",
                       (self.agent_id, self.user_id, name, json.dumps({"recovered_at": now})))
            db.execute("UPDATE jobs SET status='retry_wait',available=?,owner=NULL,lease_until=NULL WHERE id=?",
                       (now, job_id))
            return True

    def cancel_job(self, job_id, *, reason="cancelled by operator", now=None):
        return self._operator_update(job_id, "cancelled", error=reason, now=now)

    def _operator_update(self, job_id, status, *, clear_result=False, error=None, now=None):
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=? AND agent=? AND user=?",
                             (job_id, self.agent_id, self.user_id)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] == "running" and (row["lease_until"] or 0) > now:
                raise ValueError("job has an active lease; stop its worker or wait for expiry")
            if row["status"] in {"succeeded", "committed"}:
                raise ValueError("completed job cannot be retried or cancelled")
            db.execute("UPDATE jobs SET status=?,error=?,available=?,owner=NULL,lease_until=NULL,result=? WHERE id=?",
                       (status, error, now, None if clear_result else row["result"], job_id))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    @staticmethod
    def _budget_day(now):
        return datetime.fromtimestamp(now, timezone.utc).date().isoformat()

    def _prepare_budget(self, db, now):
        # Import pre-ledger usage once, across all domains sharing this runtime DB.
        # New gateway logs identify their reservation and must never be counted twice.
        marker = db.execute("SELECT value FROM budget_meta WHERE name='legacy_usage_cursor'").fetchone()
        cursor = marker[0] if marker else 0
        rows = db.execute("SELECT id,payload FROM usage WHERE id>? ORDER BY id", (cursor,)).fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            if not payload.get("budget_reservation_id"):
                usage = payload.get("usage", {})
                charged = int(payload.get("charged_tokens", 0) or usage.get("total_tokens", 0) or
                              usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
                if charged > 0 and payload.get("provider") and payload.get("day"):
                    db.execute("INSERT OR IGNORE INTO budget_reservations VALUES (?,?,?,?,?,'settled',?,?)",
                               (f"legacy-usage-{row['id']}", payload.get("budget_scope") or payload["provider"], payload["day"],
                                charged, charged, now, now))
        if rows:
            db.execute("INSERT OR REPLACE INTO budget_meta VALUES ('legacy_usage_cursor',?)", (rows[-1]["id"],))
        # Expiry changes the operational status, never refunds an unknown bill.
        db.execute("UPDATE budget_reservations SET status='unknown',charged_tokens=reserved_tokens WHERE status='reserved' AND expires_at<=?", (now,))

    def reserve_budget(self, scope, estimated_tokens, daily_limit, *, ttl_seconds=300, now=None):
        """Atomically reserve one provider attempt; None means the UTC day is full.

        Scope is intentionally independent of agent/user. Callers using the same
        provider account must share this DB and scope. Expired/uncertain attempts
        retain their conservative charge for their original day.
        """
        if (not scope or not isinstance(scope, str) or
                not isinstance(estimated_tokens, int) or isinstance(estimated_tokens, bool) or
                not isinstance(daily_limit, int) or isinstance(daily_limit, bool) or
                estimated_tokens < 0 or daily_limit < 0 or not 1 <= ttl_seconds <= 3600):
            raise ValueError("invalid shared budget reservation")
        now = time.time() if now is None else now
        day = self._budget_day(now)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prepare_budget(db, now)
            total = db.execute("SELECT COALESCE(SUM(COALESCE(charged_tokens,reserved_tokens)),0) FROM budget_reservations WHERE scope=? AND day=?",
                               (scope, day)).fetchone()[0]
            if total + estimated_tokens > daily_limit:
                return None
            reservation_id = uid("budget")
            db.execute("INSERT INTO budget_reservations VALUES (?,?,?,?,NULL,'reserved',?,?)",
                       (reservation_id, scope, day, estimated_tokens, now + ttl_seconds, now))
            return reservation_id

    def settle_budget(self, reservation_id, charged_tokens=None, *, status=None, now=None):
        """Settle known usage or conservatively retain a possibly billed attempt.

        A late response may resolve an expired/unknown reservation. Repeating an
        identical settlement is harmless; changing known usage is rejected.
        """
        status = status or ("unknown" if charged_tokens is None else "settled")
        if status not in {"settled", "unknown"} or (charged_tokens is not None and (
                not isinstance(charged_tokens, int) or isinstance(charged_tokens, bool) or charged_tokens < 0)):
            raise ValueError("invalid budget settlement")
        now = time.time() if now is None else now
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM budget_reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise KeyError(reservation_id)
            charged = row["reserved_tokens"] if charged_tokens is None else int(charged_tokens)
            if status == "unknown":
                charged = max(charged, row["reserved_tokens"])
            if row["status"] == "settled":
                if row["charged_tokens"] != charged or status != "settled":
                    raise ValueError("known budget settlement is immutable")
                return
            db.execute("UPDATE budget_reservations SET charged_tokens=?,status=? WHERE id=?",
                       (charged, status, reservation_id))

    def budget_usage(self, scope, *, now=None):
        now = time.time() if now is None else now
        day = self._budget_day(now)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._prepare_budget(db, now)
            row = db.execute("SELECT COALESCE(SUM(CASE WHEN status='reserved' THEN reserved_tokens ELSE 0 END),0), COALESCE(SUM(CASE WHEN status!='reserved' THEN charged_tokens ELSE 0 END),0) FROM budget_reservations WHERE scope=? AND day=?",
                             (scope, day)).fetchone()
            return {"day": day, "reserved_tokens": row[0], "used_tokens": row[1]}

    def get_meta(self, name, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM schedule WHERE agent=? AND user=? AND name=?", (self.agent_id, self.user_id, name)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, name, value):
        self.set_meta_many({name: value})

    def set_meta_many(self, values):
        with self.db() as db:
            db.executemany("INSERT OR REPLACE INTO schedule VALUES (?,?,?,?)",
                [(self.agent_id, self.user_id, name, json.dumps(value)) for name, value in values.items()])

    def log_usage(self, payload):
        with self.db() as db:
            db.execute("INSERT INTO usage(agent,user,payload) VALUES (?,?,?)", (self.agent_id, self.user_id, json.dumps(payload, ensure_ascii=False)))

    def usage(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM usage WHERE agent=? AND user=?", (self.agent_id, self.user_id))]

    def save_protocol(self, turn_id, payload):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO protocols VALUES(?,?,?,?)", (self.agent_id, self.user_id, turn_id, json.dumps(payload, ensure_ascii=False)))

    def get_protocol(self, turn_id):
        with self.db() as db:
            row = db.execute("SELECT payload FROM protocols WHERE agent=? AND user=? AND turn_id=?", (self.agent_id, self.user_id, turn_id)).fetchone()
            return json.loads(row[0]) if row else None
