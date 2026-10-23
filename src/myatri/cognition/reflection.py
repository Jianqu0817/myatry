from myatri.contracts import Record, uid
from .tasks import cognitive_call, proposal_for


class ReflectionAgent:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway

    async def propose(self, refs):
        snapshot, sources, data, generator, run = await cognitive_call(
            self.store, self.gateway, "reflection", refs,
            "A5：比较共同模式与差异、检索到的反例。反思不算新的外部经历，不以次数改变人格。"
            "claims每条必须引用本次输入的来源，区分事实、猜测和不确定处。")
        records = [run]
        available = {r.ref for r in sources}
        if data["decision"] == "propose":
            if not data.get("claims"):
                raise ValueError("empty reflection proposal")
            for claim in data["claims"]:
                if not claim["evidence_refs"] or not set(claim["evidence_refs"]) <= available:
                    raise ValueError("reflection claim cites unavailable source")
            if not set(data.get("counter_refs", [])) <= available:
                raise ValueError("unknown reflection counterexample")
            record = Record(record_id=uid("reflection"), record_kind="reflection", agent_id=self.store.agent_id,
                            user_id=self.store.user_id, author_kind="model", generator=generator,
                            source_refs=list(available) + [run.ref], body_markdown="\n".join(c["text"] for c in data["claims"]),
                            payload={**data, "snapshot_id": snapshot, "selected_subgraph": list(available),
                                     "material_limitations": run.payload["materials"]["limitations"]})
            records.append(record)
        return proposal_for(snapshot, sources, records, "A5", run)
