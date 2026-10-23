"""A durable working-window boundary; source archives are never erased."""
from datetime import datetime, timezone
import json


class SessionWindow:
    def __init__(self, store, queue):
        self.store, self.queue = store, queue
        self._snapshot, self._excluded = None, set()

    def state(self):
        return self.queue.get_meta("context_window_boundary")

    def _excluded_ids(self):
        state = self.state()
        snapshot = state.get("snapshot") if state else None
        if snapshot != self._snapshot:
            self._excluded = {record.record_id for record in self.store.list_records(snapshot=snapshot)} if snapshot else set()
            self._snapshot = snapshot
        return self._excluded

    def filter_records(self, records):
        excluded = self._excluded_ids()
        return [record for record in records if record.record_id not in excluded]

    def summary_allowed(self, summary):
        excluded = self._excluded_ids()
        return (summary.record_id not in excluded and not any(
            ref.rsplit("@", 1)[0] in excluded for ref in summary.payload.get("source_range", [])))

    def clear(self):
        state = {"snapshot": self.store.snapshot(), "cleared_at": datetime.now(timezone.utc).isoformat()}
        # Retire interrupted protocol payloads in the same transaction as the
        # boundary. Keep their original messages available for historical audit.
        with self.queue.db() as db:
            db.executemany("INSERT OR REPLACE INTO schedule VALUES (?,?,?,?)", [
                (self.queue.agent_id, self.queue.user_id, "context_window_boundary", json.dumps(state)),
                (self.queue.agent_id, self.queue.user_id, "context_token_anchor", "null")])
            protocols = list(db.execute("SELECT turn_id,payload FROM protocols WHERE agent=? AND user=?",
                                        (self.queue.agent_id, self.queue.user_id)))
            for row in protocols:
                payload = json.loads(row[1])
                if payload.get("status") == "active":
                    payload.update(status="context_reset", reset_at=state["cleared_at"])
                    db.execute("UPDATE protocols SET payload=? WHERE agent=? AND user=? AND turn_id=?",
                               (json.dumps(payload, ensure_ascii=False), self.queue.agent_id, self.queue.user_id, row[0]))
        return state
