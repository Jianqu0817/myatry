"""Resume the original main conversation when its Harness tool has returned."""
import asyncio
from contextlib import closing
import hashlib
import json
import sqlite3
import time

from .bridge import Incoming


class ComputerReports:
    def __init__(self, bridge):
        self.bridge = bridge
        with self.db() as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS computer_reports (
                task TEXT, turn INTEGER, delivery_id TEXT UNIQUE, state TEXT, attempts INTEGER DEFAULT 0,
                next_attempt REAL DEFAULT 0, payload TEXT, PRIMARY KEY(task,turn))''')
            # Supersede unsent independent drafts. Never rewrite a confirmed or
            # uncertain platform send, and never execute historical tasks again.
            db.execute("UPDATE deliveries SET status='superseded' WHERE id LIKE 'pc_report_%' AND status='prepared'")
            db.execute("UPDATE computer_reports SET state='superseded' WHERE delivery_id LIKE 'pc_report_%' "
                       "AND state IN ('pending','generating','prepared') AND delivery_id IN "
                       "(SELECT id FROM deliveries WHERE status='superseded')")
            db.execute("UPDATE computer_reports SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'waiting' END "
                       "WHERE state='generating' AND delivery_id NOT LIKE 'pc_report_%'")

    def db(self):
        return closing(sqlite3.connect(self.bridge.path, timeout=10))

    def status(self, task):
        with self.db() as db:
            row = db.execute('SELECT r.delivery_id,r.state,r.attempts,d.status,d.sent_parts '
                'FROM computer_reports r LEFT JOIN deliveries d ON d.id=r.delivery_id '
                'WHERE r.task=? ORDER BY r.turn DESC LIMIT 1', (task,)).fetchone()
        if row is None:
            return {'state': 'waiting_for_task'}
        return dict(zip(('delivery_id', 'state', 'attempts', 'delivery_state', 'sent_parts'), row, strict=True))

    def wait(self, message, task_id):
        """Bind completion to the original user delivery, with no extra reply."""
        try:
            turn = self.bridge.computer_agent.store.get(task_id)['turn']
        except KeyError:
            turn = 0  # Main call is checkpointed; execution has not been submitted yet.
        with self.db() as db, db:
            db.execute('INSERT OR IGNORE INTO computer_reports(task,turn,delivery_id,state,payload) '
                       "VALUES(?,?,?,'waiting',?)", (task_id, turn, message.key,
                       json.dumps(message.model_dump(), ensure_ascii=False)))
            db.execute("UPDATE deliveries SET status='waiting_for_computer',trigger_kind='computer_report' "
                       "WHERE id=? AND status IN ('received','waiting_for_computer')", (message.key,))

    async def tick(self):
        bridge = self.bridge
        store = bridge.computer_agent.store
        if not store or not bridge.computer.live:
            return
        # Recover a crash after the main tool-call checkpoint but before wait().
        # Ordinary received turns are not regenerated, and old background tasks
        # are never converted into newly addressed chat messages.
        with self.db() as db:
            interrupted = db.execute("SELECT envelope FROM deliveries WHERE status='received' "
                                     "AND envelope IS NOT NULL ORDER BY rowid DESC LIMIT 20").fetchall()
        for (encoded,) in interrupted:
            source = json.loads(encoded)
            if not bridge.computer.authorized(source):
                continue
            message = Incoming.model_validate({'mentioned': False,
                **{k: v for k, v in source.items() if k in Incoming.model_fields}})
            task_id = hashlib.sha256(json.dumps([message.platform_id, message.bot_id,
                message.group_id, message.message_id], ensure_ascii=False).encode()).hexdigest()
            try:
                row = store.get(task_id)
            except KeyError:
                # The conversation checkpoint and delivery ledger are separate
                # databases. Recover the narrow gap before wait registration,
                # without loading persona/indexes for ordinary failed turns.
                runtime = bridge.root / message.scope / 'runtime.sqlite3'
                if runtime.is_file():
                    with closing(sqlite3.connect(runtime.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                        saved = db.execute('SELECT payload FROM protocols WHERE turn_id=?', (message.key,)).fetchone()
                    if saved:
                        protocol = json.loads(saved[0])
                        if protocol.get('status') == 'active' and protocol.get('computer_wait'):
                            self.wait(message, task_id)
                continue
            if json.loads(row['contract']).get('reply_mode') == 'conversation':
                self.wait(message, task_id)
        with self.db() as db:
            pending = db.execute("SELECT r.task,r.turn,r.delivery_id,r.payload FROM computer_reports r "
                "JOIN deliveries d ON d.id=r.delivery_id WHERE r.state='waiting' AND r.attempts<3 "
                "AND r.next_attempt<=? AND d.status='waiting_for_computer' ORDER BY r.rowid LIMIT 10",
                (time.time(),)).fetchall()
        for task, turn, key, encoded in pending:
            try:
                row = store.get(task)
            except KeyError:
                row = None  # Crash after the call checkpoint, before submission.
            if row and row['state'] not in {'completed', 'blocked', 'unknown', 'cancelled', 'paused'}:
                continue
            source = json.loads(encoded)
            if not bridge.computer.authorized(source) or (row and row['owner'] != source['sender_id']):
                continue
            message = Incoming.model_validate(source)
            if row and message.scope != row['scope']:
                continue
            # Retry the original frozen conversation protocol, not a second
            # persona request. All result/data and final reply remain on that turn.
            with self.db() as db, db:
                changed = db.execute("UPDATE computer_reports SET state='generating',attempts=attempts+1 "
                                     "WHERE task=? AND turn=? AND state='waiting'", (task, turn)).rowcount
                envelope = db.execute('SELECT envelope FROM deliveries WHERE id=?', (key,)).fetchone()
            if not changed:
                continue
            try:
                result = await bridge._generate(message, envelope[0])
                with self.db() as db, db:
                    db.execute('UPDATE computer_reports SET state=? WHERE task=? AND turn=?',
                               ('prepared' if result['status'] == 'prepared' else 'waiting', task, turn))
                    if result['status'] == 'waiting_for_computer':
                        db.execute('UPDATE computer_reports SET attempts=attempts-1 WHERE task=? AND turn=?', (task, turn))
            except asyncio.CancelledError:
                raise
            except Exception:
                with self.db() as db, db:
                    db.execute("UPDATE computer_reports SET state=CASE WHEN attempts>=3 THEN 'failed' ELSE 'waiting' END,"
                               "next_attempt=? WHERE task=? AND turn=?", (time.time()+60, task, turn))
            return

    async def loop(self):
        # Bind HTTP before cold-loading archived conversations.
        await asyncio.sleep(1)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(2)
