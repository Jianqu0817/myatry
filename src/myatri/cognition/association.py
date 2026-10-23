import json

from myatri.contracts import Edge, Record, uid
from .tasks import cognitive_call, proposal_for
from .materials import select_materials


ASSOCIATION_SCHEMA = {"type": "object", "required": ["decision", "reason", "edges"], "properties": {
    "decision": {"enum": ["propose", "defer", "reject"]}, "reason": {"type": "string"},
    "edges": {"type": "array", "maxItems": 30, "items": Edge.model_json_schema()}}, "additionalProperties": False}


class AssociationAgent:
    def __init__(self, store, gateway, index):
        self.store, self.gateway, self.index = store, gateway, index

    async def propose(self, refs):
        available = set(refs)
        pairs = [e for e in self.index.graph.edges if e["type"] == "similar" and
                 (e["from_ref"] in available or e["to_ref"] in available)][:30]
        candidates = list(dict.fromkeys([*refs, *[e[k] for e in pairs for k in ("from_ref", "to_ref")]]))
        selection = select_materials(self.store, candidates)
        selected_refs = {r.ref for r in selection.sources}
        pairs = [e for e in pairs if e["from_ref"] in selected_refs and e["to_ref"] in selected_refs]
        allowed_pairs = {frozenset((e["from_ref"], e["to_ref"])) for e in pairs}
        snapshot, sources, data, generator, run = await cognitive_call(
            self.store, self.gateway, "association", candidates,
            "A2：只对以下近邻候选建立有来源语义假设，不把相似当支持。区分supports/contradicts/follows_up/"
            "reinterprets/causal_hypothesis，不生成similar。每条边必须有本次材料evidence_refs。没有完整候选pair时defer。\n" +
            json.dumps([{k: edge[k] for k in ("from_ref", "to_ref", "type")} for edge in pairs]), ASSOCIATION_SCHEMA,
            selection=selection)
        records = [run]
        source_refs = {r.ref for r in sources}
        for raw in data["edges"] if data["decision"] == "propose" else []:
            edge = Edge.model_validate(raw)
            if (edge.type == "similar" or not edge.evidence_refs or
                    frozenset((edge.from_ref, edge.to_ref)) not in allowed_pairs or
                    not set([edge.from_ref, edge.to_ref, *edge.evidence_refs]) <= source_refs):
                raise ValueError("association outside bounded evidence candidates")
            records.append(Record(record_id=edge.edge_id, record_kind="edge", agent_id=self.store.agent_id,
                                  user_id=self.store.user_id, author_kind="model", generator=generator,
                                  source_refs=list(dict.fromkeys([edge.from_ref, edge.to_ref, *edge.evidence_refs, run.ref])),
                                  body_markdown=f"{edge.from_ref} {edge.type} {edge.to_ref}", payload=edge.model_dump()))
        return proposal_for(snapshot, sources, records, "A2", run)
