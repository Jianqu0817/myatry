from myatri.contracts import Record, TemporaryEntry, uid
from myatri.persona.temporary import event_roots, resolve
from .tasks import DECISION_SCHEMA, cognitive_call, proposal_for
from copy import deepcopy


TEMPORARY_SCHEMA = deepcopy(DECISION_SCHEMA)
entry_schema = TemporaryEntry.model_json_schema()
entry_schema["properties"]["status"] = {"const": "open", "default": "open"}
entry_schema["properties"]["persona_change_ref"] = {"type": "null", "default": None}
entry_schema["required"] = list(dict.fromkeys([*entry_schema.get("required", []), "origin_kind", "origin_refs"]))
TEMPORARY_SCHEMA["properties"]["temporary_entries"] = {"type": "array", "maxItems": 8, "items": entry_schema}
TEMPORARY_SCHEMA["properties"]["updates"] = {"type": "array", "maxItems": 8, "items": {
    "type": "object", "required": ["entry_ref", "reason", "evidence_refs"], "additionalProperties": False,
    "properties": {"entry_ref": {"type": "string"}, "status": {"enum": ["open", "paused", "settled", "discarded"]},
                   "text": {"type": "string", "minLength": 1}, "reason": {"type": "string", "minLength": 1},
                   "evidence_refs": {"type": "array", "items": {"type": "string"}}}}}


class TemporaryStateAgent:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway

    async def propose(self, refs):
        snapshot, sources, data, generator, run = await cognitive_call(
            self.store, self.gateway, "temporary_state", refs,
            "A8：整理念头/情感余波/暂定意图/未决问题。可共存矛盾、维持、缩小范围、暂缓或放弃。"
            "temporary_entries使用TemporaryEntry字段。updates用entry_ref,status,text,reason,evidence_refs。"
            "generated_idea的origin_refs可留空，程序引用生成run；experience必须引用输入经历。"
            "无真实行动不可称已尝试，无反馈保持未知。不得直接consolidated。",
            TEMPORARY_SCHEMA, prompt_version="v2")
        available = {r.ref for r in sources}
        records = [run]
        for raw in data.get("temporary_entries", []) if data["decision"] == "propose" else []:
            raw = dict(raw)
            if raw.get("origin_kind") == "generated_idea":
                raw["origin_refs"] = [run.ref]
            entry = TemporaryEntry.model_validate(raw)
            if entry.status != "open":
                raise ValueError("new candidate must be open")
            if entry.origin_kind == "experience":
                if not set(entry.origin_refs) <= available or not event_roots(self.store, entry.origin_refs, snapshot=snapshot):
                    raise ValueError("state cites no external input event roots")
            records.append(Record(record_id=uid("state"), record_kind="temporary_state", agent_id=self.store.agent_id,
                                  user_id=self.store.user_id, author_kind="model", generator=generator,
                                  source_refs=entry.origin_refs, body_markdown=entry.text, payload=entry.model_dump()))
        for update in data.get("updates", []) if data["decision"] == "propose" else []:
            if update["entry_ref"] not in available:
                raise ValueError("state update outside read set")
            prior = resolve(self.store, update["entry_ref"], snapshot)
            if prior.record_kind != "temporary_state" or prior.payload["status"] in {"discarded", "consolidated"}:
                raise ValueError("cannot update retired state")
            if update.get("status") == "consolidated":
                raise ValueError("only persona commit consolidates")
            evidence = update.get("evidence_refs", [])
            if not set(evidence) <= available:
                raise ValueError("unknown update evidence")
            entry = TemporaryEntry.model_validate({**prior.payload, **{k: update[k] for k in ("status", "text", "reason") if k in update}})
            records.append(Record(record_id=prior.record_id, revision=prior.revision+1, supersedes_revision=prior.revision,
                                  record_kind="temporary_state", agent_id=self.store.agent_id, user_id=self.store.user_id,
                                  author_kind="model", generator=generator, source_refs=[prior.ref, run.ref, *evidence],
                                  body_markdown=entry.text, payload=entry.model_dump()))
        return proposal_for(snapshot, sources, records, "A8", run)
