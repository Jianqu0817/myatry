"""Bridge-only candidate isolation. Original identity/evidence validators still run."""
import json

from myatri.cognition.association import AssociationAgent as CoreAssociation, ASSOCIATION_SCHEMA
from myatri.cognition.experience import ExperienceOrganizer as CoreOrganizer, _group_item, _group_source
from myatri.cognition.materials import select_materials
from myatri.cognition.tasks import cognitive_call, proposal_for
from myatri.contracts import Edge, Record, digest


class UnresolvedCandidates(ValueError):
    """No validated candidate remains; do not mark the source as organized."""


class ExperienceOrganizer(CoreOrganizer):
    def _audit(self, rejected, refs):
        identifier = "bridge_quarantine_" + digest([refs, rejected])[:24]
        try:
            return self.store.get_record(identifier)
        except KeyError:
            row = Record(record_id=identifier, record_kind="cognitive_run", agent_id=self.store.agent_id,
                user_id=self.store.user_id, source_refs=list(dict.fromkeys(refs)),
                payload={"role": "bridge_memory_validation", "decision": "quarantined",
                         "rejected_candidates": rejected})
            self.store.commit([row], identifier, expected={identifier: 0})
            return row

    def _candidate_error(self, message, item, shown=None):
        source = _group_source(message)
        quote = item.get("quote")
        if not isinstance(quote, str) or not quote or quote not in (source["text"] if source else message.body_markdown):
            return "quote_not_exact"
        try:
            normalized, attribution = _group_item(message, item)
        except ValueError as error:
            return getattr(error, "code", "invalid_attribution")
        prior_ref = item.get("existing_event_ref")
        if not prior_ref:
            return None
        if shown is not None and prior_ref not in shown:
            return "unshown_prior_event"
        try:
            rid, revision = prior_ref.rsplit("@", 1)
            prior = self.store.get_record(rid, int(revision))
        except (KeyError, ValueError, AttributeError):
            return "missing_prior_event"
        if prior.record_kind != "event" or prior.payload.get("reality_scope") != item.get("reality_scope"):
            return "prior_event_scope_mismatch"
        if source:
            if sorted(prior.payload.get("participants", [])) != sorted(normalized["participants"]):
                return "merge_member_ownership"
            previous = [r.payload.get("attribution") for r in self.store.list_records("interpretation")
                        if prior.ref in r.payload.get("event_refs", [])]
            if not any(isinstance(old, dict) and all(old.get(k) == attribution.get(k)
                    for k in ("speaker_id", "subject_id", "attribution")) for old in previous):
                return "merge_attribution_ownership"
        return None

    async def _draft_response(self, request, identifier, enabled):
        if request.purpose != "A1_batch":
            return await super()._draft_response(request, identifier, enabled)
        # Keep the original request budget; allow a bounded longer JSON response.
        request = request.model_copy(update={"max_tokens": max(request.max_tokens, 8192)})
        response = await super()._draft_response(request, identifier, enabled)
        source = json.loads(request.messages[-1]["content"])
        windows = source["messages"]
        shown = {r["ref"] for r in source.get("existing_events", [])}
        data = json.loads(response.content)
        accepted, rejected = [], []
        refs = list(dict.fromkeys(r["ref"] for r in windows))
        for index, item in enumerate(data["events"]):
            ref = item.get("message_ref")
            matches = [row for row in windows if row["ref"] == ref]
            code = "quote_outside_named_window" if not any(item["quote"] in row["text"] for row in matches) else None
            if not code:
                rid, rev = ref.rsplit("@", 1)
                code = self._candidate_error(self.store.get_record(rid, int(rev)), item, shown)
            if code:
                rejected.append({"index": index, "message_ref": ref if ref in refs else refs[0],
                    "status": "unconfirmed", "reason_code": code, "candidate": item})
            else:
                accepted.append(item)
        if rejected:
            audit = self._audit(rejected, refs)
            # Core checkpoints retain this extra provenance when committing per-message runs.
            self._bridge_audits = [*getattr(self, "_bridge_audits", []), audit]
            if not accepted:
                raise UnresolvedCandidates("all A1 candidates quarantined; sources remain pending")
        return response.model_copy(update={"content": json.dumps({"events": accepted}, ensure_ascii=False)})

    async def propose(self, message, *, fixture=None, extraction=None):
        if extraction is None:
            return await super().propose(message, fixture=fixture, extraction=extraction)
        data, checkpoints = extraction
        accepted, rejected = [], []
        for index, item in enumerate(data["events"]):
            code = self._candidate_error(message, item)
            if code:
                rejected.append({"index": index, "message_ref": message.ref, "status": "unconfirmed",
                    "reason_code": code, "candidate": item})
            else:
                accepted.append(item)
        audits = list(getattr(self, "_bridge_audits", []))
        # Recover audit provenance after a crash between a saved batch and per-message commits.
        audits.extend(r for r in self.store.list_records("cognitive_run")
            if r.payload.get("role") == "bridge_memory_validation" and message.ref in r.source_refs)
        if rejected:
            audits.append(self._audit(rejected, [message.ref]))
            if not accepted:
                raise UnresolvedCandidates("all A1 candidates quarantined; sources remain pending")
        combined = {r.ref: r for r in [*audits, *checkpoints]}
        return await super().propose(message, fixture=fixture,
            extraction=({"events": accepted}, list(combined.values())))


class AssociationAgent(CoreAssociation):
    async def propose(self, refs):
        available = set(refs)
        pairs = [e for e in self.index.graph.edges if e["type"] == "similar"
                 and (e["from_ref"] in available or e["to_ref"] in available)][:30]
        candidates = list(dict.fromkeys([*refs, *[e[k] for e in pairs for k in ("from_ref", "to_ref")]]))
        selection = select_materials(self.store, candidates)
        selected = {r.ref for r in selection.sources}
        pairs = [e for e in pairs if {e["from_ref"], e["to_ref"]} <= selected]
        allowed = {frozenset((e["from_ref"], e["to_ref"])) for e in pairs}
        snapshot, sources, data, generator, run = await cognitive_call(self.store, self.gateway,
            "association", candidates, "只对以下候选对建立语义假设；相似不等于支持。不得生成similar。"
            "from_ref、to_ref必须逐字使用同一候选对；evidence_refs只能来自本次材料。无完整候选时defer。\n" +
            json.dumps([{k: e[k] for k in ("from_ref", "to_ref", "type")} for e in pairs]),
            ASSOCIATION_SCHEMA, selection=selection)
        source_refs = {r.ref for r in sources}
        accepted, rejected, seen = [], [], set()
        for raw in data["edges"] if data["decision"] == "propose" else []:
            edge = Edge.model_validate(raw)
            if (edge.type == "similar" or not edge.evidence_refs or edge.edge_id in seen
                    or frozenset((edge.from_ref, edge.to_ref)) not in allowed
                    or not {edge.from_ref, edge.to_ref, *edge.evidence_refs} <= source_refs):
                rejected.append({"reason_code": "association_outside_candidates", "candidate": raw})
                continue
            # Generated IDs must not overwrite or merge a pre-existing archive object.
            try:
                self.store.get_record(edge.edge_id)
            except KeyError:
                pass
            else:
                rejected.append({"reason_code": "association_existing_id", "candidate": raw})
                continue
            try:
                record = Record(record_id=edge.edge_id, record_kind="edge", agent_id=self.store.agent_id,
                    user_id=self.store.user_id, author_kind="model", generator=generator,
                    source_refs=list(dict.fromkeys([edge.from_ref, edge.to_ref, *edge.evidence_refs, run.ref])),
                    body_markdown=f"{edge.from_ref} {edge.type} {edge.to_ref}", payload=edge.model_dump())
            except ValueError:
                rejected.append({"reason_code": "association_invalid_record", "candidate": raw})
                continue
            seen.add(edge.edge_id)
            accepted.append(record)
        if rejected:
            value = run.model_dump()
            value["payload"].update(rejected_candidates=rejected,
                decision="propose_with_quarantine" if accepted else "defer_with_quarantine")
            value["content_hash"] = ""
            run = Record.model_validate(value)
        records = [run, *accepted]
        return proposal_for(snapshot, sources, records, "A2", run)
