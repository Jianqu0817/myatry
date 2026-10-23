"""Promote attributed claims with original evidence, without another model call."""
from datetime import datetime, timezone

from myatri.contracts import Event, Record, SourceSpan, digest
from myatri.retrieval import RecallService


class SummaryArchive:
    def __init__(self, store, identities, scope):
        self.store, self.identities, self.scope = store, identities, scope

    def save(self, summary, messages):
        identifier = "listen_summary_" + digest([self.scope, summary["window_end"]])[:40]
        source = {}
        for message in messages:
            message_id = message["message_id"]
            if message_id in source and source[message_id] != message:
                raise ValueError("duplicate original message ID has conflicting evidence")
            source[message_id] = message
        try:
            prior_run = self.store.get_record(identifier)
            if prior_run.payload.get("summary") != summary:
                raise ValueError("summary window reused with different content")
            for ref in prior_run.source_refs:
                original = self.store.get_record(ref.rsplit("@", 1)[0], int(ref.rsplit("@", 1)[1]))
                channel = original.payload["channel_source"]
                if source.get(channel["message_id"]) != channel:
                    raise ValueError("original listening evidence changed")
            return
        except KeyError:
            pass
        personal = [item for item in summary["items"] if item["attribution"] in {"self_report", "reported"}]
        for item in personal:
            source_ids = set(item["source_message_ids"])
            quote_ids = {quote["message_id"] for quote in item["source_quotes"]}
            if not source_ids or source_ids != quote_ids or not source_ids.issubset(source):
                raise ValueError("memory source IDs must match exact quoted message IDs")
            checked = self.identities.validate_memory(self.scope, {
                **item, "evidence": item["source_quotes"]}, messages)
            if not checked["valid"]:
                raise ValueError("invalid long-term member attribution: " + checked["reason"])
        needed = {ref for item in personal for ref in item["source_message_ids"]}
        records, raw_refs = [], {}
        for message_id in sorted(needed):
            message = source[message_id]
            raw_id = "listen_" + digest([self.scope, message_id])[:40]
            raw = Record(record_id=raw_id, record_kind="raw_message", agent_id=self.store.agent_id,
                         user_id=self.store.user_id, author_kind="human",
                         created_at=datetime.fromisoformat(message["sent_at"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(),
                         body_markdown=message["text"], payload={"role": "user", "channel_source": message,
                            "logical_source_key": digest([self.scope, message_id]),
                            "evidence_not_independent_of_same_message_id": True})
            raw_refs[message_id] = raw.ref
            try:
                prior = self.store.get_record(raw_id)
                if prior != raw:
                    raise ValueError("original listening evidence changed")
            except KeyError:
                records.append(raw)
        run = Record(record_id=identifier, record_kind="cognitive_run", agent_id=self.store.agent_id,
                     user_id=self.store.user_id, author_kind="model", generator=summary["generator"],
                     source_refs=list(raw_refs.values()), body_markdown="带来源的群聊短期摘要与成员自述/转述候选。",
                     payload={"role": "group_short_summary", "summary": summary})
        records.append(run)
        for message_id, ref in raw_refs.items():
            checkpoint_id = "run_exp_" + digest({"message": ref, "version": "v1"})[:24]
            try:
                self.store.get_record(checkpoint_id)
            except KeyError:
                records.append(Record(record_id=checkpoint_id, record_kind="cognitive_run",
                    agent_id=self.store.agent_id, user_id=self.store.user_id, source_refs=[ref, run.ref],
                    payload={"role": "A1", "decision": "covered_by_validated_group_summary",
                             "source_coverage": {"complete": True, "scope": "original_member_text"}}))
        for i, item in enumerate(personal):
            spans = []
            for quote in item["source_quotes"]:
                message = source[quote["message_id"]]
                start = message["text"].index(quote["quote"])
                spans.append(SourceSpan(message_ref=raw_refs[quote["message_id"]], start=start,
                                        end=start + len(quote["quote"]), quote=quote["quote"]))
            fact = Event(occurred_at=None, participants=list(dict.fromkeys([item["speaker_id"], item["subject_id"]])),
                         fact_summary=f"[{item['attribution']}，仅为该成员的自述或转述] {item['text']}",
                         source_spans=spans, event_fingerprint=digest([run.ref, i]), reality_scope="reported")
            event = Record(record_id="group_fact_" + digest([run.ref, i])[:40], record_kind="event",
                           agent_id=self.store.agent_id, user_id=self.store.user_id, author_kind="model",
                           generator=summary["generator"], source_refs=[*dict.fromkeys(s.message_ref for s in spans), run.ref],
                           body_markdown=fact.fact_summary, payload=fact.model_dump())
            records.extend([event, Record(record_id="group_claim_" + digest([run.ref, i])[:40],
                record_kind="interpretation", agent_id=self.store.agent_id, user_id=self.store.user_id,
                author_kind="model", generator=summary["generator"], source_refs=[event.ref, run.ref],
                body_markdown=fact.fact_summary, payload={"group_attribution": item, "verified_external_fact": False,
                    "claim_state": {"facet": item.get("facet"), "change": item.get("change", "assertion"),
                        "supersedes_message_ids": item.get("supersedes_message_ids", []),
                        "valid_time": item.get("valid_time"), "observed_at": [source[ref]["sent_at"] for ref in item["source_message_ids"]],
                        "support_review": summary.get("review", {}).get("status", "not_reviewed")}})])
        self.store.commit(records, idempotency_key=identifier, expected={r.record_id: 0 for r in records})


class GroupRecall(RecallService):
    """Use plain current text for search; keep personal questions with their owner."""
    def __init__(self, store, index, identities, scope):
        super().__init__(store, index)
        self.identities, self.scope, self.current_sender = identities, scope, ""

    def recall(self, query, filters=None, snapshot=None, budget=4000):
        import json
        import re
        try:
            envelope = json.loads(query)
        except (ValueError, TypeError):
            envelope = None
        if isinstance(envelope, dict) and "sender_id" in envelope:
            query = envelope.get("text", "")
        if not self.current_sender:
            return super().recall(query, filters, snapshot, budget)
        personal = bool(self.current_sender and (re.search(r"我(?!们)|咱|本人", query)
            or (re.search(r"还记得|记不记得|上次|那次", query) and not re.search(r"群里|大家|我们|别人", query))))
        subject = self.current_sender
        member_ids = [r["member_id"] for r in self.identities.context(self.scope, self.current_sender,
            [r.payload.get("channel_source", {}).get("sender_id", "") for r in self.store.list_records("raw_message", snapshot=snapshot)])["members"]]
        members = self.identities.context(self.scope, self.current_sender, member_ids)["members"]
        names = {name for member in members for name in [member["display_name"], *member["aliases"]]
                 if name and name not in {"我", "你", "他", "她", "我们", "大家", "自己", "本人", "我的", "上次"}
                 and re.search(r"(?:关于|记得|提到|说说)" + re.escape(name) + r"|" + re.escape(name)
                               + r"(?:的|喜欢|讨厌|说过|答应|是谁)", query)}
        named_ids = {self.identities.resolve_name(self.scope, name).get("member_id") for name in names}
        named_ids.discard(None)
        if len(named_ids) == 1:
            subject = next(iter(named_ids))
            personal = True
        elif names:
            uncertain = super().recall(query, filters, snapshot, budget)
            return uncertain.model_copy(update={"limitations": [*uncertain.limitations,
                "查询提到的名字可能对应多个成员；只提供来源，不认证同名身份。"]})
        if not personal:
            return super().recall(query, filters, snapshot, budget)
        snapshot = snapshot or self.store.snapshot()
        latest = self.store.list_records(snapshot=snapshot)
        event_refs = {record.ref for record in latest if record.record_kind == "event"}
        event_subjects, companions = {}, {}
        for interpretation in latest:
            if interpretation.record_kind != "interpretation":
                continue
            claims = interpretation.payload.get("group_attribution") or interpretation.payload.get("attribution", {})
            if not isinstance(claims, dict) or not claims:
                continue
            related = [*interpretation.payload.get("event_refs", []), *interpretation.source_refs]
            for ref in related:
                if ref in event_refs:
                    event_subjects.setdefault(ref, set()).add(claims.get("subject_id") or None)
                    companions.setdefault(ref, []).append(interpretation.ref)
        raw_subjects = {}
        for event in latest:
            if event.ref in event_subjects:
                for span in event.payload.get("source_spans", []):
                    raw_subjects.setdefault(span["message_ref"], set()).update(event_subjects[event.ref])
        eligible = set()
        for record in latest:
            payload = record.payload
            claim = payload.get("group_attribution") or payload.get("attribution", {})
            if claim:
                people = {claim.get("subject_id")}
            elif record.ref in event_subjects:
                people = event_subjects[record.ref]
                if len(people) != 1:
                    people = set()
            elif record.ref in raw_subjects:
                people = raw_subjects[record.ref]
            else:
                people = set(payload.get("participants", []))
                people.add(payload.get("channel_source", {}).get("sender_id"))
                if record.record_kind == "raw_message" and payload.get("role") == "user":
                    try:
                        original = json.loads(record.body_markdown)
                    except ValueError:
                        original = None
                    if isinstance(original, dict):
                        people.add(original.get("sender_id"))
            if payload.get("source_owner") == self.identities.owner_name and self.identities.owner_id:
                people.add(self.identities.owner_id)
            if subject in people:
                eligible.add(record.ref)
        selected_filters = {**(filters or {}), "candidate_refs": sorted(eligible),
                            "evidence_companions": companions}
        bundle = super().recall(query, selected_filters, snapshot, budget)
        # Keep complete source bundles. A quoted source's speaker can differ from
        # the remembered subject; it remains evidence, not a claim about the speaker.
        supported = {span["message_ref"] for record in bundle.records if record.ref in eligible
                     for span in record.payload.get("source_spans", [])}
        supported.update(ref for record in bundle.records if record.ref in eligible
                         for ref in companions.get(record.ref, []))
        raw_refs = {record.ref for record in bundle.records if record.record_kind == "raw_message"}
        supported.update(ref for path in bundle.paths if path[0] in eligible for ref in path[1:] if ref in raw_refs)
        kept, used = [], 0
        for record in bundle.records:
            if record.ref not in eligible | supported:
                continue
            size = len(record.body_markdown) + len(str(record.payload))
            if used + size <= budget:
                kept.append(record)
                used += size
        refs = {record.ref for record in kept}
        return bundle.model_copy(update={"records": kept,
            "reasons": {ref: reasons for ref, reasons in bundle.reasons.items() if ref in refs},
            "paths": [path for path in bundle.paths if all(ref in refs for ref in path)],
            "counter_refs": [ref for ref in bundle.counter_refs if ref in refs],
            "limitations": [*bundle.limitations, "按当前发言成员筛选个人回忆，未知归属不冒认。"]})
