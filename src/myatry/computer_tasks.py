"""Durable owner-bound task queue; raw execution data never becomes group memory."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time

TERMINAL = {"completed", "blocked", "cancelled", "unknown"}


class TaskStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,owner TEXT,scope TEXT,goal TEXT,
                    state TEXT,created REAL,updated REAL,turn INTEGER DEFAULT 0,summary TEXT DEFAULT '',
                    calls INTEGER DEFAULT 0,tokens INTEGER DEFAULT 0,prompt_hash TEXT,contract TEXT);
                CREATE TABLE IF NOT EXISTS events(task TEXT,seq INTEGER,payload TEXT,PRIMARY KEY(task,seq));
                CREATE TABLE IF NOT EXISTS receipts(task TEXT,call_id TEXT,fingerprint TEXT,state TEXT,result TEXT,
                    PRIMARY KEY(task,call_id));
                CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,fingerprint TEXT,result TEXT);
                CREATE TABLE IF NOT EXISTS inputs(task TEXT,turn INTEGER,text TEXT,PRIMARY KEY(task,turn));
                CREATE TABLE IF NOT EXISTS catalog(task TEXT,name TEXT,definition TEXT,PRIMARY KEY(task,name));
            """)

    def db(self):
        return _Connection(self.path)

    def catalog(self, task, tools=None):
        with self.db() as db:
            if tools is not None:
                db.execute("DELETE FROM catalog WHERE task=?", (task,))
                db.executemany(
                    "INSERT INTO catalog VALUES(?,?,?)",
                    [
                        (task, t["name"], json.dumps(t, ensure_ascii=False))
                        for t in tools
                    ],
                )
            return {
                r[0]: json.loads(r[1])
                for r in db.execute(
                    "SELECT name,definition FROM catalog WHERE task=?", (task,)
                )
            }

    def result(self, task, call_id):
        with self.db() as db:
            row = db.execute(
                "SELECT result FROM receipts WHERE task=? AND call_id=? AND state='completed'",
                (task, call_id),
            ).fetchone()
        if row is None:
            raise ValueError("unconfirmed_receipt")
        return json.loads(row[0])

    def receipt_ids(self, task):
        """Actual native receipts, excluding finish/error receipts as evidence."""
        with self.db() as db:
            rows = db.execute("SELECT call_id,result FROM receipts WHERE task=? AND state='completed' "
                              "ORDER BY rowid DESC", (task,)).fetchall()
        return [r[0] for r in rows if isinstance(json.loads(r[1] or '{}').get('isError'), bool)][:12]

    def completion_errors(self, task):
        with self.db() as db:
            rows = db.execute("SELECT result FROM receipts WHERE task=?", (task,)).fetchall()
        return sum(json.loads(r[0] or '{}').get('error') == 'completion_verification_failed' for r in rows)

    def save_draft(self, task, text):
        with self.db() as db:
            db.execute("UPDATE tasks SET summary=? WHERE id=? AND state='running'", (text[:1000], task))

    def report_summary(self, task, summary):
        if not summary.startswith('执行回合已停止；以工具回执及独立检查为准。'):
            return summary
        with self.db() as db:
            for (payload,) in db.execute('SELECT payload FROM events WHERE task=? ORDER BY seq DESC LIMIT 100', (task,)):
                event = json.loads(payload)
                if event.get('type') != 'assistant/message':
                    continue
                blocks = event.get('data', {}).get('message', {}).get('content', [])
                text = blocks if isinstance(blocks, str) else '\n'.join(
                    b.get('text', '') for b in blocks if isinstance(b, dict) and b.get('type') == 'text')
                if text.strip():
                    return '执行报告（尚未完成校验，摘自原始会话记录）：\n' + text[:1000]
        return summary

    def feedback(self, owner, scope, *, task=None):
        """Bounded internal CC; no raw terminal output, file content or reasoning."""
        def text(value, limit):
            value = re.sub(r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})",
                           "[已隐藏密钥]", str(value))
            value = re.sub(r"(?i)Bearer\s+\S+", "Bearer [已隐藏]", value)
            return value[:limit]

        reports = []
        with self.db() as db:
            rows = db.execute("SELECT id,goal,state,summary,updated FROM tasks WHERE owner=? AND scope=? "
                              + ('AND id=? ' if task else '') + "ORDER BY updated DESC LIMIT 3",
                              (owner, scope, task) if task else (owner, scope)).fetchall()
            for task, goal, state, summary, updated in rows:
                actions = []
                for (payload,) in db.execute("SELECT payload FROM events WHERE task=? ORDER BY seq DESC LIMIT 100", (task,)):
                    event = json.loads(payload)
                    if event.get('type') != 'tool/call':
                        continue
                    data = event.get('data', {})
                    if data.get('name') == 'finish_task':
                        continue
                    args = data.get('arguments', {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    receipt = db.execute('SELECT state,result FROM receipts WHERE task=? AND call_id=?',
                                         (task, data.get('callId', ''))).fetchone()
                    result = json.loads(receipt[1] or '{}') if receipt else {}
                    value = result.get('value') or {}
                    actions.append({'tool': text(data.get('name', ''), 80),
                                    'purpose': text(args.get('description', ''), 120),
                                    'receipt_state': receipt[0] if receipt else 'unconfirmed',
                                    'is_error': result.get('isError'),
                                    'exit_code': value.get('exitCode') if isinstance(value, dict) else None})
                    if len(actions) == 4:
                        break
                last = db.execute("SELECT result FROM receipts WHERE task=? AND state='completed' "
                                  "ORDER BY rowid DESC LIMIT 1", (task,)).fetchone()
                last = json.loads(last[0] or '{}') if last else {}
                reports.append({'task_id': task, 'goal': text(goal, 300), 'state': state,
                                'summary': text(self.report_summary(task, summary), 1000), 'updated': updated,
                                'verification': {k: last[k] for k in ('state', 'error', 'reason', 'checks') if k in last},
                                'recent_actions': list(reversed(actions))})
        return reports

    def recover(self):
        with self.db() as db:
            # Interrupted effects cannot safely be replayed; explicit human follow-up is required.
            db.execute(
                "UPDATE tasks SET state='unknown',updated=? WHERE state IN ('running','pausing','cancelling')",
                (time.time(),),
            )
            db.execute("UPDATE receipts SET state='unknown' WHERE state='running'")

    def create(self, identifier, owner, scope, goal, contract, prompt_hash):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT owner,scope,goal FROM tasks WHERE id=?", (identifier,)
            ).fetchone()
            if old:
                if tuple(old) != (owner, scope, goal):
                    raise ValueError("task identity conflict")
                return identifier
            stamp = time.time()
            db.execute(
                "INSERT INTO tasks(id,owner,scope,goal,state,created,updated,prompt_hash,contract) "
                "VALUES(?,?,?,?,'queued',?,?,?,?)",
                (
                    identifier,
                    owner,
                    scope,
                    goal,
                    stamp,
                    stamp,
                    prompt_hash,
                    json.dumps(contract, ensure_ascii=False),
                ),
            )
            db.execute("INSERT INTO inputs VALUES(?,0,?)", (identifier, goal))
        return identifier

    def next(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id FROM tasks WHERE state='queued' ORDER BY created LIMIT 1"
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE tasks SET state='running',updated=? WHERE id=?",
                    (time.time(), row[0]),
                )
                return row[0]

    def pending_count(self):
        with self.db() as db:
            return db.execute(
                "SELECT COUNT(*) FROM tasks WHERE state IN ('queued','running','pausing','cancelling')"
            ).fetchone()[0]

    def tool_count(self, task):
        with self.db() as db:
            return db.execute(
                "SELECT COUNT(*) FROM receipts WHERE task=?", (task,)
            ).fetchone()[0]

    def finish(self, task, call_id, result, summary):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE receipts SET state='completed',result=? WHERE task=? AND call_id=?",
                (json.dumps(result, ensure_ascii=False), task, call_id),
            )
            # A concurrent human pause/cancel wins over model completion.
            db.execute(
                "UPDATE tasks SET state=?,summary=?,updated=? WHERE id=? AND state='running'",
                (result["state"], summary, time.time(), task),
            )

    def input(self, task):
        with self.db() as db:
            return db.execute(
                "SELECT text FROM inputs WHERE task=? ORDER BY turn DESC LIMIT 1",
                (task,),
            ).fetchone()[0]

    def control(
        self, task, action, *, command_id, text="继续原目标；先重新观察当前状态。"
    ):
        fingerprint = hashlib.sha256(
            json.dumps([task, action, text], ensure_ascii=False).encode()
        ).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT fingerprint,result FROM commands WHERE id=?", (command_id,)
            ).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError("command_conflict")
                return json.loads(old[1])
            row = db.execute(
                "SELECT state,turn FROM tasks WHERE id=?", (task,)
            ).fetchone()
            if row is None:
                raise KeyError(task)
            state, turn = row
            if action == "pause" and state in {"queued", "running"}:
                state = "paused" if state == "queued" else "pausing"
            elif action == "cancel" and state in {
                "queued",
                "paused",
                "blocked",
                "running",
                "pausing",
            }:
                state = "cancelling" if state in {"running", "pausing"} else "cancelled"
            elif action == "resume" and state in {"paused", "blocked"}:
                state = "queued"
                turn += 1
                db.execute("INSERT INTO inputs VALUES(?,?,?)", (task, turn, text))
            else:
                raise ValueError("task_state_conflict")
            db.execute(
                "UPDATE tasks SET state=?,turn=?,updated=? WHERE id=?",
                (state, turn, time.time(), task),
            )
            result = {"task_id": task, "state": state}
            db.execute(
                "INSERT INTO commands VALUES(?,?,?)",
                (command_id, fingerprint, json.dumps(result)),
            )
            return result

    def charge(self, task, calls, tokens):
        with self.db() as db:
            db.execute(
                "UPDATE tasks SET calls=calls+?,tokens=tokens+?,updated=? WHERE id=?",
                (calls, tokens, time.time(), task),
            )

    def details(self, task):
        row = self.get(task)
        row['summary'] = self.report_summary(task, row['summary'])
        with self.db() as db:
            receipts = [
                {
                    "call_id": r[0],
                    "state": r[1],
                    "result": json.loads(r[2]) if r[2] else None,
                }
                for r in db.execute(
                    "SELECT call_id,state,result FROM receipts WHERE task=? ORDER BY rowid",
                    (task,),
                )
            ]
        return {
            k: row[k]
            for k in (
                "id",
                "goal",
                "state",
                "created",
                "updated",
                "calls",
                "tokens",
                "turn",
                "summary",
                "prompt_hash",
            )
        } | {"contract": json.loads(row["contract"]), "receipts": receipts}

    def get(self, task):
        with self.db() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task,)).fetchone()
        if row is None:
            raise KeyError(task)
        return dict(row)

    def list(self):
        with self.db() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT id,state,created,updated,calls,tokens,turn FROM tasks ORDER BY created DESC LIMIT 50"
            ).fetchall()
        return [dict(row) for row in rows]

    def state(self, task, state, summary=None):
        with self.db() as db:
            db.execute(
                "UPDATE tasks SET state=?,updated=?,summary=COALESCE(?,summary) WHERE id=?",
                (state, time.time(), summary, task),
            )

    def events(self, task):
        with self.db() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT payload FROM events WHERE task=? ORDER BY seq", (task,)
                )
            ]

    def event(self, task, event):
        with self.db() as db:
            db.execute(
                "INSERT INTO events VALUES(?,?,?)",
                (task, event["seq"], json.dumps(event, ensure_ascii=False)),
            )

    def claim_call(self, task, call_id, name, args):
        fingerprint = hashlib.sha256(
            json.dumps([name, args], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT fingerprint,state,result FROM receipts WHERE task=? AND call_id=?",
                (task, call_id),
            ).fetchone()
            if old:
                if old[0] != fingerprint:
                    raise ValueError("call identity conflict")
                return False, json.loads(old[2]) if old[1] == "completed" else {
                    "error": "outcome_unknown"
                }
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?,'running',NULL)",
                (task, call_id, fingerprint),
            )
        return True, None

    def receipt(self, task, call_id, result, *, unknown=False):
        with self.db() as db:
            db.execute(
                "UPDATE receipts SET state=?,result=? WHERE task=? AND call_id=?",
                (
                    "unknown" if unknown else "completed",
                    json.dumps(result, ensure_ascii=False),
                    task,
                    call_id,
                ),
            )


class _Connection:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.db = sqlite3.connect(self.path, timeout=5)
        return self.db

    def __exit__(self, kind, error, tb):
        with closing(self.db):
            self.db.rollback() if kind else self.db.commit()
