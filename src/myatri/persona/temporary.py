from __future__ import annotations

from myatri.contracts import BehaviorAttempt, Record, TemporaryEntry, digest, parse_ref, parse_timestamp, uid, utcnow


def resolve(store, ref, snapshot=None):
    record_id, revision = parse_ref(ref)
    return store.get_record(record_id, revision=revision, snapshot=snapshot)


def event_roots(store, refs, *, snapshot=None):
    roots, seen = set(), set()
    def visit(ref):
        if ref in seen:
            return
        seen.add(ref)
        record = resolve(store, ref, snapshot)
        # A self-authored change/reflection/run is never independent external evidence.
        if record.record_kind == "event":
            if record.payload.get("reality_scope") in {"reported", "observed"}:
                roots.add(record.ref)
            return
        if record.record_kind in {"interpretation", "temporary_state", "behavior_attempt"}:
            for source in record.source_refs:
                visit(source)
    for ref in refs:
        visit(ref)
    return sorted(roots)


def observed_action_time(store, attempt):
    """Feedback cannot predate either the recorded or actual assistant action."""
    moments = [parse_timestamp(attempt.payload.get("occurred_at"))]
    for ref in attempt.payload.get("action_refs", []):
        action = resolve(store, ref)
        if action.record_kind != "raw_message" or action.payload.get("role") != "assistant":
            raise ValueError("feedback action source must be a persisted assistant message")
        moments.append(parse_timestamp(action.created_at))
    return max(moments)


class TemporaryStates:
    def __init__(self, store):
        self.store = store

    def create(self, entry: TemporaryEntry, *, entry_id=None, generator=None):
        if entry.status == "consolidated":
            raise ValueError("only atomic persona commit can consolidate")
        for ref in entry.origin_refs:
            source = resolve(self.store, ref)
            if entry.origin_kind == "generated_idea" and source.record_kind != "cognitive_run":
                raise ValueError("generated idea must reference a generation run")
        if entry.origin_kind == "experience" and not event_roots(self.store, entry.origin_refs):
            raise ValueError("experience state requires an external event root")
        record = Record(record_id=entry_id or uid("state"), record_kind="temporary_state",
                        agent_id=self.store.agent_id, user_id=self.store.user_id,
                        author_kind="model" if generator else "system", generator=generator,
                        source_refs=entry.origin_refs, body_markdown=entry.text, payload=entry.model_dump())
        self.store.commit([record], idempotency_key="state-create-" + record.record_id, expected={record.record_id: 0})
        return record

    def review(self, entry_id, *, base_revision, status=None, text=None, reason, evidence_refs=None):
        current = self.store.get_record(entry_id)
        if current.record_kind != "temporary_state" or current.revision != base_revision:
            raise ValueError("stale or invalid temporary state")
        if status == "consolidated":
            raise ValueError("only atomic persona commit can consolidate")
        if current.payload["status"] in {"discarded", "consolidated"} and status is not None and status != current.payload["status"]:
            raise ValueError("retired state cannot silently reactivate; create a new linked state")
        data = {**current.payload, "reason": reason}
        if status:
            data["status"] = status
        if text is not None:
            data["text"] = text
        entry = TemporaryEntry.model_validate(data)
        refs = list(dict.fromkeys([current.ref, *(evidence_refs or []), *entry.origin_refs]))
        record = Record(record_id=entry_id, revision=base_revision + 1, supersedes_revision=base_revision,
                        record_kind="temporary_state", agent_id=current.agent_id, user_id=current.user_id,
                        source_refs=refs, body_markdown=entry.text, payload=entry.model_dump())
        self.store.commit([record], idempotency_key="state-review-" + digest([record.ref, record.content_hash]), expected={entry_id: base_revision})
        return record

    def list(self, status=None, snapshot=None):
        return [r for r in self.store.list_records("temporary_state", snapshot=snapshot) if not status or r.payload["status"] == status]

    def prepare_attempt_feedback(self, attempt: BehaviorAttempt, *, attempt_id=None, base_revision=None,
                                 generator=None, source_refs=(), action_snapshot=None):
        """Validate a behavior record without publishing, for atomic model proposals."""
        intent = resolve(self.store, attempt.intent_ref)
        current_intent = self.store.get_record(intent.record_id)
        prior = None
        if base_revision is not None:
            prior = self.store.get_record(attempt_id)
            if prior.record_kind != "behavior_attempt" or prior.revision != base_revision or prior.payload["intent_ref"] != attempt.intent_ref:
                raise ValueError("stale attempt or changed intent")
            if prior.payload.get("action_refs") and (attempt.action_refs != prior.payload["action_refs"] or attempt.occurred_at != prior.payload.get("occurred_at")):
                raise ValueError("feedback cannot rewrite an observed action")
        new_action = prior is None or not prior.payload.get("action_refs")
        historical_action = False
        if action_snapshot and new_action:
            then = self.store.get_record(intent.record_id, snapshot=action_snapshot)
            historical_action = then.ref == intent.ref and then.payload.get("status") == "open"
            for ref in attempt.action_refs:
                action = resolve(self.store, ref)
                if action.payload.get("context_snapshot") != action_snapshot:
                    raise ValueError("historical expression must pin the answer's context snapshot")
        if intent.record_kind != "temporary_state" or (new_action and not historical_action and (current_intent.ref != intent.ref or current_intent.payload["status"] != "open")):
            raise ValueError("attempt requires a current open intent")
        if attempt.decision_status in {"attempted", "expressed"}:
            for ref in attempt.action_refs:
                action = resolve(self.store, ref)
                if action.record_kind != "raw_message" or action.payload.get("role") != "assistant":
                    raise ValueError("conversation action must reference a persisted assistant message")
            if not attempt.occurred_at:
                raise ValueError("observed action requires occurrence time")
        for ref in attempt.observed_feedback_refs:
            feedback = resolve(self.store, ref)
            if feedback.record_kind != "event" or feedback.payload.get("reality_scope") not in {"reported", "observed"}:
                raise ValueError("feedback must be external event evidence")
        revision, previous = 1, None
        if base_revision is not None:
            revision, previous = base_revision + 1, base_revision
        refs = [attempt.intent_ref, *attempt.action_refs, *attempt.observed_feedback_refs, *([prior.ref] if prior else [])]
        record = Record(record_id=attempt_id or uid("attempt"), revision=revision, supersedes_revision=previous,
                        record_kind="behavior_attempt", agent_id=self.store.agent_id, user_id=self.store.user_id,
                        author_kind="model" if generator else "system", generator=generator,
                        source_refs=list(dict.fromkeys([*refs, *source_refs])), body_markdown=attempt.decision_status, payload=attempt.model_dump())
        expected = {record.record_id: base_revision or 0}
        if new_action and not historical_action:
            expected[intent.record_id] = current_intent.revision
        return record, expected

    def record_attempt_feedback(self, attempt: BehaviorAttempt, *, attempt_id=None, base_revision=None):
        record, expected = self.prepare_attempt_feedback(attempt, attempt_id=attempt_id, base_revision=base_revision)
        self.store.commit([record], idempotency_key="attempt-" + digest([record.ref, record.content_hash]),
                          expected=expected)
        return record
