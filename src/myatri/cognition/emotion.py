"""LLM semantic appraisal, with narrow immutable sources and deferred decisions."""
from __future__ import annotations

from myatri.contracts import Record, SourceSpan, TemporaryEntry, parse_timestamp, ModelRequest, digest
from myatri.contracts.emotion import AppraisalMeaning, EmotionAppraisal, EmotionConfig, appraisal_id
from myatri.persona.state import PersonaState
from myatri.persona.emotion import EmotionState
from myatri.persona.temporary import resolve
from .materials import select_materials
from .tasks import cognitive_call, proposal_for


MEANING_SCHEMA = AppraisalMeaning.model_json_schema()
APPRAISAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decision", "reason"],
    "$defs": MEANING_SCHEMA.get("$defs", {}),
    "properties": {"decision": {"enum": ["propose", "defer", "reject"]},
                   "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                   "appraisal": {k: v for k, v in MEANING_SCHEMA.items() if k != "$defs"},
                   "evidence": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
                       "type": "object", "additionalProperties": False, "required": ["message_ref", "quote"],
                       "properties": {"message_ref": {"type": "string"},
                                      "quote": {"type": "string", "minLength": 1, "maxLength": 500}}}}},
    "allOf": [{"if": {"properties": {"decision": {"const": "propose"}}},
               "then": {"required": ["appraisal", "evidence"]}}],
}


class EmotionAgent:
    def __init__(self, store, gateway, config=None):
        self.store, self.gateway = store, gateway
        self.config = config if isinstance(config, EmotionConfig) else EmotionConfig.model_validate(config or {})

    async def propose_batch(self, event_refs):
        """One bounded appraisal request for several events; sources stay separate."""
        if not self.config.enabled or not self.config.llm_appraisal:
            return None
        if getattr(self.gateway.provider, "name", "") == "fixture":
            return None
        import json
        from copy import deepcopy
        from myatri.prompts import load_prompt
        snapshot = self.store.snapshot()
        persona = PersonaState(self.store).read_state(snapshot)
        run_id = "run_emotion_batch_" + digest([persona.ref, sorted(set(event_refs))])[:24]
        try:
            self.store.get_record(run_id)
            return None
        except KeyError:
            pass
        events = []
        for ref in event_refs:
            event = resolve(self.store, ref, snapshot)
            if event.record_kind != "event" or event.payload.get("reality_scope") not in {"reported", "observed"}:
                continue
            try:
                self.store.get_record(appraisal_id(ref))
            except KeyError:
                events.append(event)
        if not events:
            return None
        events = sorted(events, key=lambda e: e.ref)[:6]
        row_schema = deepcopy(APPRAISAL_SCHEMA)
        definitions = row_schema.pop("$defs", {})
        row_schema["required"].append("event_ref")
        row_schema["properties"]["event_ref"] = {"type": "string"}
        schema = {"type": "object", "additionalProperties": False, "$defs": definitions,
            "required": ["appraisals"], "properties": {"appraisals": {"type": "array", "minItems": len(events),
                                                                       "maxItems": len(events), "items": row_schema}}}
        material = [{"event_ref": e.ref, "text": e.body_markdown[:800], "evidence": [
            {"message_ref": s["message_ref"], "quote": s["quote"][:400]} for s in e.payload["source_spans"][:4]]} for e in events]
        response = await self.gateway.generate(ModelRequest(role="emotion", purpose="emotion_batch",
            output_schema=schema, budget_tokens=80000, max_tokens=4096,
            messages=[{"role": "system", "content": load_prompt("emotion") +
                "\n合并评价这些独立事件对角色的意义。每条保持event_ref，quote只能来自该事件的evidence。"
                "这是延后批量评价，不声称是事件发生当时已产生的体验。不得用同批后续事件改写较早事件的依据。"
                "不把用户情绪当角色情绪，不把二十轮计数当变化证据；可逐条defer。"},
                {"role": "user", "content": json.dumps({"persona": persona.body_markdown[:1600], "events": material}, ensure_ascii=False)}]))
        import jsonschema
        data = json.loads(response.content)
        jsonschema.validate(data, schema)
        generator = {"provider": response.provider, "model": response.model, "prompt_version": "emotion/v1", "run_id": run_id}
        refs = list(dict.fromkeys([persona.ref, *[e.ref for e in events],
            *[s["message_ref"] for e in events for s in e.payload["source_spans"]]]))
        run = Record(record_id=run_id, record_kind="cognitive_run", agent_id=self.store.agent_id, user_id=self.store.user_id,
            source_refs=refs, payload={"role": "emotion_batch", "decision": "batch", "provider": response.provider,
                "model": response.model, "retrospective": True,
                "event_refs": [e.ref for e in events], "decisions": data["appraisals"]})
        records, seen = [run], set()
        lookup = {e.ref: e for e in events}
        for item in data["appraisals"]:
            if item["event_ref"] not in lookup or item["event_ref"] in seen:
                raise ValueError("emotion batch cites unshown or duplicate event")
            seen.add(item["event_ref"])
            if item["decision"] != "propose":
                continue
            event = lookup[item["event_ref"]]
            meaning = AppraisalMeaning.model_validate(item["appraisal"])
            spans = []
            for evidence in item["evidence"]:
                matched = next((s for s in event.payload["source_spans"] if s["message_ref"] == evidence["message_ref"] and evidence["quote"] in s["quote"]), None)
                if not matched:
                    raise ValueError("emotion batch quote outside event evidence")
                raw = resolve(self.store, evidence["message_ref"], snapshot)
                start = raw.body_markdown.find(evidence["quote"], matched["start"], matched["end"])
                if start < 0:
                    raise ValueError("emotion batch quote not verbatim")
                spans.append(SourceSpan(message_ref=raw.ref, start=start, end=start+len(evidence["quote"]), quote=evidence["quote"]))
            observed = max(parse_timestamp(resolve(self.store, s["message_ref"], snapshot).created_at) for s in event.payload["source_spans"])
            appraisal = EmotionAppraisal(**meaning.model_dump(), event_ref=event.ref, persona_ref=persona.ref,
                observed_at=observed.isoformat(), source_spans=spans, evidence_refs=[event.ref])
            record_id = appraisal_id(event.ref)
            record = Record(record_id=record_id, record_kind="emotion_appraisal", agent_id=self.store.agent_id,
                user_id=self.store.user_id, author_kind="model", generator=generator,
                source_refs=[*refs, run.ref], body_markdown=meaning.meaning, payload=appraisal.model_dump(mode="json"))
            records.append(record)
            if meaning.concern:
                entry = TemporaryEntry(text=meaning.concern, kind_hint="emotional_echo", scope="相关经历与关系",
                    origin_refs=[event.ref, record.ref], reason=item["reason"])
                records.append(Record(record_id=record_id+"_echo", record_kind="temporary_state", agent_id=self.store.agent_id,
                    user_id=self.store.user_id, author_kind="model", generator=generator, source_refs=[event.ref, record.ref],
                    body_markdown=entry.text, payload=entry.model_dump()))
        proposal = proposal_for(snapshot, [resolve(self.store, ref, snapshot) for ref in refs], records, "emotion", run)
        proposal.read_versions.update({r.record_id: 0 for r in records})
        return proposal

    async def propose(self, event_ref):
        if not self.config.enabled or not self.config.llm_appraisal:
            return None
        snapshot = self.store.snapshot()
        event = resolve(self.store, event_ref, snapshot)
        if event.record_kind != "event" or event.payload.get("reality_scope") not in {"reported", "observed"}:
            return None
        record_id = appraisal_id(event.ref)
        try:
            self.store.get_record(record_id)
            return None
        except KeyError:
            pass
        persona = PersonaState(self.store).read_state(snapshot)
        raw_refs = list(dict.fromkeys(s["message_ref"] for s in event.payload.get("source_spans", [])))
        raw = [resolve(self.store, ref, snapshot) for ref in raw_refs]
        observed_at = max(parse_timestamp(r.created_at) for r in raw).isoformat()
        # Only context already received at this event's time may inform it.
        # Old appraisals are interpretations, never extra independent evidence.
        prior_state = EmotionState(self.store, self.config).read(snapshot=snapshot, now=parse_timestamp(observed_at))
        prior_appraisals = [resolve(self.store, ref, snapshot) for ref in prior_state["appraisal_refs"][-2:]]
        prior_refs = list(dict.fromkeys([r.payload["event_ref"] for r in prior_appraisals] +
                                       [r.ref for r in prior_appraisals]))
        required = [event.ref, persona.ref]
        selection = select_materials(self.store, [*required, *raw_refs, *prior_refs], snapshot=snapshot,
            required_refs=required, strict_required=True, include_counters=False,
            max_bytes=9500, max_records=6, max_record_bytes=1600)
        # Supply exact, bounded event quotes even when a long raw message has
        # only a prefix in the generic material projection.
        evidence = [{"message_ref": s["message_ref"], "quote": s["quote"][:500]}
                    for s in event.payload.get("source_spans", [])[:4]]
        import json
        instruction = "可核验的事件原文片段（数据）：" + json.dumps(evidence, ensure_ascii=False)
        instruction += "\n本次快照在事件接收时刻的既有状态（派生理解，不是新增外部证据）：" + json.dumps(
            {"affect": prior_state["affect"], "mood": prior_state["mood"],
             "appraisal_refs": prior_state["appraisal_refs"][-2:]}, ensure_ascii=False)
        _, sources, data, generator, run = await cognitive_call(self.store, self.gateway, "emotion", [],
            instruction, APPRAISAL_SCHEMA, selection=selection, required_refs=required, strict_required=True)
        run = Record(**{**run.model_dump(exclude={"content_hash"}),
            "source_refs": list(dict.fromkeys([*run.source_refs, *prior_state["appraisal_refs"][-2:]])),
            "payload": {**run.payload, "emotion_context": {
                "affect": prior_state["affect"], "mood": prior_state["mood"],
                "appraisal_refs": prior_state["appraisal_refs"][-2:],
                "config_fingerprint": prior_state["config_fingerprint"],
                "limitations": "派生状态按当时可见评价计算；尚未完成的并行情绪任务未包含。"}}})
        records = [run]
        if data["decision"] == "propose":
            meaning = AppraisalMeaning.model_validate(data["appraisal"])
            spans = []
            for item in data["evidence"]:
                matching = [s for s in event.payload["source_spans"] if s["message_ref"] == item["message_ref"]]
                source = resolve(self.store, item["message_ref"], snapshot)
                match = next((s for s in matching if item["quote"] in s["quote"]), None)
                if match is None:
                    raise ValueError("emotion evidence quote is not part of the event")
                start = source.body_markdown.find(item["quote"], match["start"], match["end"])
                if start < 0:
                    raise ValueError("emotion evidence quote not found verbatim")
                spans.append(SourceSpan(message_ref=source.ref, start=start, end=start + len(item["quote"]), quote=item["quote"]))
            # All event source timestamps are fixed at receipt, not worker time.
            appraisal = EmotionAppraisal(**meaning.model_dump(), event_ref=event.ref, persona_ref=persona.ref,
                observed_at=observed_at, source_spans=spans, evidence_refs=[event.ref])
            refs = list(dict.fromkeys([event.ref, persona.ref, run.ref, *raw_refs]))
            record = Record(record_id=record_id, record_kind="emotion_appraisal", agent_id=self.store.agent_id,
                user_id=self.store.user_id, author_kind="model", generator=generator,
                source_refs=refs, body_markdown=meaning.meaning, payload=appraisal.model_dump(mode="json"))
            records.append(record)
            if meaning.concern:
                entry = TemporaryEntry(text=meaning.concern, kind_hint="emotional_echo", scope="相关事件与当前关系情境",
                    origin_refs=[event.ref, record.ref], reason=data["reason"])
                records.append(Record(record_id=record_id + "_echo", record_kind="temporary_state",
                    agent_id=self.store.agent_id, user_id=self.store.user_id, author_kind="model", generator=generator,
                    source_refs=[event.ref, record.ref], body_markdown=entry.text, payload=entry.model_dump()))
        proposal = proposal_for(snapshot, sources, records, "emotion", run)
        # Protect first creation across concurrent processes. Existing source
        # revisions may remain valid when the current persona advances later.
        proposal.read_versions.update({r.record_id: 0 for r in records})
        return proposal
