"""Conservative, source-checked attribution of expressed intentions and feedback."""
from __future__ import annotations

import json

from myatri.contracts import BehaviorAttempt, ModelRequest, Proposal, Record, digest, uid, parse_timestamp
from myatri.persona.temporary import TemporaryStates, resolve, observed_action_time
from myatri.prompts import load_prompt
from .materials import project_record


ATTRIBUTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["decision", "reason", "expressions", "feedback"],
    "properties": {
        "decision": {"enum": ["propose", "defer"]}, "reason": {"type": "string"},
        "expressions": {"type": "array", "maxItems": 4, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["intent_ref", "action_ref", "quote"],
            "properties": {k: {"type": "string", "minLength": 1, "maxLength": 320}
                           for k in ("intent_ref", "action_ref", "quote")}}},
        "feedback": {"type": "array", "maxItems": 8, "items": {
            "type": "object", "additionalProperties": False,
            "required": ["attempt_ref", "event_ref", "quote"],
            "properties": {k: {"type": "string", "minLength": 1, "maxLength": 320}
                           for k in ("attempt_ref", "event_ref", "quote")}}},
    },
}


class AttributionAgent:
    def __init__(self, store, gateway):
        self.store, self.gateway = store, gateway

    async def propose(self, refs):
        snapshot = self.store.snapshot()
        records = [resolve(self.store, ref, snapshot) for ref in dict.fromkeys(refs)]
        actions = [r for r in records if r.record_kind == "raw_message" and r.payload.get("role") == "assistant"]
        states = [r for r in records if r.record_kind == "temporary_state" and r.payload.get("status") == "open"]
        attempts = []
        for record in records:
            if record.record_kind != "behavior_attempt" or record.payload.get("decision_status") not in {"expressed", "attempted"} or not record.payload.get("action_refs"):
                continue
            try:
                observed_action_time(self.store, record)
            except (ValueError, TypeError, AttributeError, KeyError):
                continue
            attempts.append(record)
        events = [r for r in records if r.record_kind == "event" and r.payload.get("reality_scope") in {"reported", "observed"}]
        # Relevance limits attention; never infer an action from activation alone.
        material, available, bytes_used = [], {}, 0
        for r in [*actions, *events, *attempts, *states]:
            row = {**project_record(r, max_bytes=2000), "kind": r.record_kind, "created_at": r.created_at}
            size = len(json.dumps(row, ensure_ascii=False).encode("utf-8"))
            if len(material) >= 16 or size + bytes_used > 16000:
                continue
            material.append(row)
            available[r.ref] = r
            bytes_used += size
        relevant = (any(r.ref in available for r in actions) and any(r.ref in available for r in states) or
                    any(r.ref in available for r in attempts) and any(r.ref in available for r in events))
        run_id = uid("attribution")
        if not relevant:
            data = {"decision": "defer", "reason": "无完整的表达/意图或后续反馈候选，不推断行为。", "expressions": [], "feedback": []}
            provider, model = "local-policy", "source-attribution-v1"
        else:
            request = ModelRequest(role="attribution", purpose="behavior_attribution", prompt_version="attribution/v1",
                output_schema=ATTRIBUTION_SCHEMA, max_tokens=1536, budget_tokens=24000,
                messages=[{"role": "system", "content": load_prompt("attribution")},
                    {"role": "user", "content": json.dumps(material, ensure_ascii=False)}])
            response = await self.gateway.generate(request)
            if response.tool_calls:
                raise ValueError("attribution does not authorize tools")
            data, provider, model = json.loads(response.content), response.provider, response.model
        generator = {"provider": provider, "model": model, "prompt_version": "attribution/v1", "run_id": run_id}
        run = Record(record_id=run_id, record_kind="cognitive_run", agent_id=self.store.agent_id,
            user_id=self.store.user_id, source_refs=list(available), payload={"role": "attribution", "snapshot": snapshot,
                "decision": data["decision"], "reason": data["reason"], "result": data,
                "omitted_count": len(records)-len(available),
                "note": "归因是解释；表达不是外部执行，反馈已观察不等于成功。"})
        output, expected, pending_attempts = [run], {}, set()
        states_api = TemporaryStates(self.store)
        if data["decision"] == "propose":
            for item in data["expressions"]:
                intent, action = available.get(item["intent_ref"]), available.get(item["action_ref"])
                if intent not in states or action not in actions or item["quote"] not in action.body_markdown:
                    raise ValueError("expression must cite an available intent and exact assistant quote")
                # The intention must have been open in the context actually used
                # to produce this answer; later-created intentions cannot apply.
                action_snapshot = action.payload.get("context_snapshot")
                if not action_snapshot:
                    raise ValueError("expression lacks the answer's original context snapshot")
                at_action = self.store.get_record(intent.record_id, snapshot=action_snapshot)
                if at_action.ref != intent.ref or at_action.payload.get("status") != "open":
                    raise ValueError("intent was not open when the answer was generated")
                attempt_id = "attempt_" + digest([intent.ref, action.ref])[:24]
                if attempt_id in pending_attempts:
                    continue
                pending_attempts.add(attempt_id)
                try:
                    prior = self.store.get_record(attempt_id)
                except KeyError:
                    prior = None
                if prior:
                    continue
                attempt = BehaviorAttempt(intent_ref=intent.ref, decision_status="expressed", action_refs=[action.ref],
                                          occurred_at=action.created_at, outcome_unknown=True)
                row, reads = states_api.prepare_attempt_feedback(attempt, attempt_id=attempt_id, generator=generator,
                                                                 source_refs=[run.ref], action_snapshot=action_snapshot)
                output.append(row)
                expected.update(reads)
            updates = {}
            for item in data["feedback"]:
                prior, event = available.get(item["attempt_ref"]), available.get(item["event_ref"])
                if prior not in attempts or event not in events:
                    raise ValueError("feedback outside available attempt/event candidates")
                valid = False
                for span in event.payload.get("source_spans", []):
                    raw = resolve(self.store, span["message_ref"], snapshot)
                    if (raw.payload.get("role") == "user" and item["quote"] in span["quote"] and
                        raw.body_markdown[span["start"]:span["end"]] == span["quote"] and
                        parse_timestamp(raw.created_at) > observed_action_time(self.store, prior)):
                        valid = True
                if not valid:
                    raise ValueError("feedback must quote a later persisted user source")
                updates.setdefault(prior.ref, []).append(event.ref)
            for ref, feedback in updates.items():
                prior = available[ref]
                combined = list(dict.fromkeys([*prior.payload["observed_feedback_refs"], *feedback]))
                if combined == prior.payload["observed_feedback_refs"]:
                    continue
                attempt = BehaviorAttempt.model_validate({**prior.payload,
                    "observed_feedback_refs": combined, "outcome_unknown": False})
                row, reads = states_api.prepare_attempt_feedback(attempt, attempt_id=prior.record_id,
                    base_revision=prior.revision, generator=generator, source_refs=[run.ref])
                output.append(row)
                expected.update(reads)
        for record in available.values():
            # Reads are immutable revisions. Attribution appends historical
            # interpretation; only an attempt being revised needs latest CAS.
            if record.record_kind in {"raw_message", "event"}:
                expected.setdefault(record.record_id, record.revision)
        return Proposal(base_snapshot=snapshot, read_versions=expected, records=output, role="attribution", run_id=run_id)
