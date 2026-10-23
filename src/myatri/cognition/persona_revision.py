from myatri.contracts import PersonaPatch, Record, digest, uid
from myatri.runtime.commit import Committer
from myatri.persona.patch import PersonaPatcher
from .tasks import cognitive_call, proposal_for
from .reviewer import Reviewer


REVISION_SCHEMA = {"type": "object", "required": ["decision", "reason"], "properties": {
    "decision": {"enum": ["propose", "defer", "reject"]}, "reason": {"type": "string"},
    "patch": PersonaPatch.model_json_schema()}, "additionalProperties": False}


class PersonaRevisionAgent:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway

    async def propose(self, refs):
        from .materials import select_materials
        snapshot = self.store.snapshot()
        current = self.store.list_records(snapshot=snapshot)
        required = [r.ref for r in current if r.record_kind == "persona_version"]
        searches = [r.ref for r in current if r.ref in refs and r.payload.get("role") == "counterexample_search"]
        required.extend(searches[-1:])
        # A current persona is required to propose an exact local patch; no
        # partially displayed persona may be treated as a reviewed baseline.
        selection = select_materials(self.store, refs, snapshot=snapshot,
                                     required_refs=required, strict_required=True)
        snapshot, sources, data, generator, run = await cognitive_call(
            self.store, self.gateway, "persona_revision", refs,
            "A6：仅在独立外部经历与反例检查支持时建议最小条目patch，可defer。"
            "不按事件次数机械判断，不将selected/expressed当实践成功，不把反思作为外部经历。"
            "只能列出已展示的外部event、状态、尝试与反例检索ref，材料不充分defer。", REVISION_SCHEMA,
            selection=selection, required_refs=required, strict_required=True)
        if data["decision"] == "propose":
            patch = PersonaPatch.model_validate(data.get("patch"))
            available = {r.ref for r in sources}
            cited = {f"persona@{patch.base_revision}", *patch.evidence_refs, *patch.counter_refs,
                     *patch.temporary_refs, *patch.attempt_refs, *patch.counter_search_refs}
            if not cited <= available:
                raise ValueError("persona patch cites material outside bounded read set")
        return data, proposal_for(snapshot, sources, [run], "A6", run)

    async def review_and_prepare(self, patch):
        review, proposal, review_ref = await Reviewer(self.store, self.gateway).review(patch)
        record = proposal.records[0]
        updated = Record(**{**record.model_dump(exclude={"content_hash"}),
                            "payload": {**record.payload, "patch_hash": digest(patch.model_dump(mode="json"))}})
        proposal.records = [updated]
        Committer(self.store).submit_proposal(proposal)
        if review["decision"] != "accept":
            return None, review
        return PersonaPatcher(self.store).propose(patch, review_ref, consolidate_refs=patch.temporary_refs), review


def record_counter_search(store, query, bundle):
    record = Record(record_id=uid("counter_search"), record_kind="cognitive_run", agent_id=store.agent_id,
                    user_id=store.user_id, source_refs=[r.ref for r in bundle.records],
                    payload={"role": "counterexample_search", "query": query, "snapshot": bundle.snapshot,
                             "counter_refs": bundle.counter_refs, "searched_refs": [r.ref for r in bundle.records],
                             "limitations": bundle.limitations})
    store.commit([record], idempotency_key=record.record_id)
    return record
