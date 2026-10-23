from __future__ import annotations

from myatri.contracts import PersonaPatch, Proposal, Record, TemporaryEntry, digest, uid
from .state import PersonaState
from .temporary import event_roots, resolve


class PersonaPatcher:
    def __init__(self, store):
        self.store = store

    def validate_persona_patch(self, patch: PersonaPatch, review_ref: str):
        current = PersonaState(self.store).read_state()
        if current.revision != patch.base_revision:
            raise ValueError("stale persona patch; reread evidence and generate again")
        entries = current.payload["entries"]
        if patch.entry_id not in entries or digest(entries[patch.entry_id]) != patch.old_hash:
            raise ValueError("old entry hash mismatch")
        if entries[patch.entry_id] == patch.replacement:
            raise ValueError("empty patch")
        roots = event_roots(self.store, patch.evidence_refs)
        if set(roots) != set(patch.evidence_refs):
            raise ValueError("persona evidence must explicitly list independent external event roots")
        if not roots:
            raise ValueError("no external evidence")
        for ref in patch.counter_refs:
            resolve(self.store, ref)
        if not patch.counter_search_refs:
            raise ValueError("counterexample search must be recorded even when none found")
        for ref in patch.counter_search_refs:
            search = resolve(self.store, ref)
            if search.record_kind != "cognitive_run" or search.payload.get("role") != "counterexample_search":
                raise ValueError("invalid counterexample search record")
        review = resolve(self.store, review_ref)
        result = review.payload.get("review", {})
        if review.record_kind != "cognitive_run" or review.payload.get("role") != "reviewer" or result.get("decision") != "accept" or not result.get("scope_ok") or not result.get("counterexamples_checked"):
            raise ValueError("persona proposal requires accepting scoped review")
        required = set([current.ref, *patch.evidence_refs, *patch.counter_refs, *patch.temporary_refs, *patch.attempt_refs, *patch.counter_search_refs])
        if not required <= set(review.source_refs):
            raise ValueError("review does not cover patch sources")
        # Bind the exact proposal content: changing replacement after A7 is forbidden.
        if review.payload.get("patch_hash") != digest(patch.model_dump(mode="json")):
            raise ValueError("review not bound to this patch")
        for ref in [*patch.temporary_refs, *patch.attempt_refs]:
            record = resolve(self.store, ref)
            if self.store.get_record(record.record_id).ref != ref:
                raise ValueError("stale state/attempt read by persona proposal")
        return current, roots

    def propose(self, patch: PersonaPatch, review_ref: str, *, consolidate_refs=None):
        current, roots = self.validate_persona_patch(patch, review_ref)
        change_id = uid("persona_change")
        change_ref = f"{change_id}@1"
        entries = {**current.payload["entries"], patch.entry_id: patch.replacement}
        read_refs = list(dict.fromkeys([current.ref, review_ref, *patch.evidence_refs, *patch.counter_refs,
                                      *patch.temporary_refs, *patch.attempt_refs, *patch.counter_search_refs]))
        change = Record(record_id=change_id, record_kind="persona_change", agent_id=self.store.agent_id,
                        user_id=self.store.user_id, source_refs=read_refs, body_markdown=patch.rationale,
                        payload={"before_ref": current.ref, "after_ref": f"persona@{current.revision+1}", "patch": patch.model_dump(),
                                 "event_roots": roots, "mode": "experience_revision", "review_ref": review_ref,
                                 "diff": {patch.entry_id: {"before": current.payload["entries"][patch.entry_id], "after": patch.replacement}}})
        persona = Record(record_id="persona", revision=current.revision+1, supersedes_revision=current.revision,
                         record_kind="persona_version", agent_id=self.store.agent_id, user_id=self.store.user_id,
                         source_refs=[current.ref, change_ref], body_markdown=PersonaState.render(entries),
                         payload={**current.payload, "entries": entries, "entry_hashes": {k: digest(v) for k, v in entries.items()}, "change_ref": change_ref})
        records = [change, persona]
        for ref in consolidate_refs or []:
            if ref not in patch.temporary_refs:
                raise ValueError("consolidation state must be in proposal read set")
            state = resolve(self.store, ref)
            if state.record_kind != "temporary_state" or state.payload["status"] not in {"open", "paused"}:
                raise ValueError("cannot consolidate retired state")
            entry = TemporaryEntry.model_validate({**state.payload, "status": "consolidated", "persona_change_ref": change_ref,
                                                   "reason": patch.rationale})
            records.append(Record(record_id=state.record_id, revision=state.revision+1, supersedes_revision=state.revision,
                                  record_kind="temporary_state", agent_id=self.store.agent_id, user_id=self.store.user_id,
                                  source_refs=[state.ref, change_ref], body_markdown=entry.text, payload=entry.model_dump()))
        return Proposal(base_snapshot=self.store.snapshot(), read_versions={resolve(self.store, ref).record_id: resolve(self.store, ref).revision for ref in read_refs},
                        records=records, role="A6", run_id=change_id)

    def restore(self, revision: int):
        current = PersonaState(self.store).read_state()
        prior = self.store.get_record("persona", revision=revision)
        if prior.ref == current.ref:
            raise ValueError("already current persona")
        change_id = uid("persona_change")
        change = Record(record_id=change_id, record_kind="persona_change", agent_id=self.store.agent_id,
                        user_id=self.store.user_id, author_kind="human_import", source_refs=[current.ref, prior.ref],
                        body_markdown=f"人工恢复人格版本 {revision}，保留历史。",
                        payload={"before_ref": current.ref, "after_ref": f"persona@{current.revision+1}",
                                 "mode": "human_restore", "event_roots": [], "restored_from": prior.ref})
        restored = Record(record_id="persona", revision=current.revision+1, supersedes_revision=current.revision,
                          record_kind="persona_version", agent_id=self.store.agent_id, user_id=self.store.user_id,
                          author_kind="human_import", source_refs=[current.ref, prior.ref, change.ref],
                          body_markdown=prior.body_markdown, payload={**prior.payload, "change_ref": change.ref})
        self.store.commit([change, restored], idempotency_key=change_id, expected={"persona": current.revision})
        return restored

    def human_edit(self, entry_id, replacement, reason):
        current = PersonaState(self.store).read_state()
        if entry_id not in current.payload["entries"] or not replacement.strip() or not reason.strip():
            raise ValueError("human edit requires existing entry, nonempty text and reason")
        before = current.payload["entries"][entry_id]
        if replacement == before:
            raise ValueError("empty human edit")
        change = Record(record_id=uid("persona_change"), record_kind="persona_change", agent_id=self.store.agent_id,
                        user_id=self.store.user_id, author_kind="human_import", source_refs=[current.ref],
                        body_markdown=reason, payload={"mode": "human_edit", "event_roots": [], "before_ref": current.ref,
                            "after_ref": f"persona@{current.revision+1}", "diff": {entry_id: {"before": before, "after": replacement}}})
        entries = {**current.payload["entries"], entry_id: replacement}
        persona = Record(record_id="persona", revision=current.revision+1, supersedes_revision=current.revision,
                         record_kind="persona_version", agent_id=self.store.agent_id, user_id=self.store.user_id,
                         author_kind="human_import", source_refs=[current.ref, change.ref], body_markdown=PersonaState.render(entries),
                         payload={**current.payload, "entries": entries, "entry_hashes": {k: digest(v) for k,v in entries.items()}, "change_ref": change.ref})
        self.store.commit([change, persona], idempotency_key=change.record_id, expected={"persona": current.revision})
        return persona

    def human_replace(self, entries, reason):
        """Explicit human-authorized full card edit; preserve archive lineage metadata."""
        current = PersonaState(self.store).read_state()
        if not entries or not reason.strip() or any(not value.strip() for value in entries.values()):
            raise ValueError("human replacement requires nonempty entries and reason")
        if entries == current.payload["entries"]:
            raise ValueError("empty human replacement")
        change = Record(record_id=uid("persona_change"), record_kind="persona_change",
            agent_id=self.store.agent_id, user_id=self.store.user_id, author_kind="human_import",
            source_refs=[current.ref], body_markdown=reason,
            payload={"mode": "human_edit", "event_roots": [], "before_ref": current.ref,
                "after_ref": f"persona@{current.revision+1}",
                "diff": {key: {"before": current.payload["entries"].get(key), "after": entries.get(key)}
                         for key in current.payload["entries"].keys() | entries.keys()
                         if current.payload["entries"].get(key) != entries.get(key)}})
        persona = Record(record_id="persona", revision=current.revision+1,
            supersedes_revision=current.revision, record_kind="persona_version",
            agent_id=self.store.agent_id, user_id=self.store.user_id, author_kind="human_import",
            source_refs=[current.ref, change.ref], body_markdown=PersonaState.render(entries),
            payload={**current.payload, "entries": entries,
                     "entry_hashes": {key: digest(value) for key, value in entries.items()}, "change_ref": change.ref})
        self.store.commit([change, persona], idempotency_key=change.record_id,
                          expected={"persona": current.revision})
        return persona
