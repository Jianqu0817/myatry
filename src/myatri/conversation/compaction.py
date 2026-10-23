from __future__ import annotations

import asyncio
from copy import deepcopy
import json

import jsonschema

from myatri.contracts import ModelRequest, Record, SourceSpan, digest, uid
from myatri.prompts import load_prompt
from myatri.providers.gateway import ProviderError, validate_protocol
from myatri.storage.archive import ConflictError


COMPACTION_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decision", "reason", "items"],
    "properties": {
        "decision": {"enum": ["summarize", "defer"]}, "reason": {"type": "string"},
        "items": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "additionalProperties": False, "required": ["category", "text", "evidence"],
            "properties": {
                "category": {"enum": ["agreement", "preference", "open_question", "fact", "change", "uncertainty"]},
                "text": {"type": "string", "minLength": 1, "maxLength": 240},
                "evidence": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
                    "type": "object", "additionalProperties": False, "required": ["ref", "quote"],
                    "properties": {"ref": {"type": "string"}, "quote": {"type": "string", "minLength": 1, "maxLength": 160}}}}}}}}}


class Compactor:
    def __init__(self, store, gateway=None, *, llm_enabled=True, chunk_bytes=9000, max_chunks=8,
                 detail_items=6, detail_chars=240, output_tokens=2048):
        self.store, self.gateway, self.llm_enabled = store, gateway, llm_enabled
        if chunk_bytes < 1000 or max_chunks < 1:
            raise ValueError("invalid compaction material limits")
        self.chunk_bytes, self.max_chunks = chunk_bytes, max_chunks
        if not 6 <= detail_items <= 40 or not 240 <= detail_chars <= 1200 or not 2048 <= output_tokens <= 16000:
            raise ValueError("invalid semantic detail limits")
        self.schema = deepcopy(COMPACTION_SCHEMA)
        self.schema["properties"]["items"]["maxItems"] = detail_items
        self.schema["properties"]["items"]["items"]["properties"]["text"]["maxLength"] = detail_chars
        self.output_tokens = output_tokens
        self.prompt_version = "v2" if detail_items > 6 else "v1"
        self._lock = asyncio.Lock()
        self.before_compact = None
        self.filter_records = lambda records: records
        self.summary_allowed = lambda summary: True

    def _prepare(self, snapshot, preserve_ids):
        raw = self.filter_records(self.store.list_records("raw_message", snapshot=snapshot))
        preserve_ids = set(preserve_ids or [])
        available = {r.record_id for r in raw} | {r.ref for r in raw}
        if not preserve_ids <= available:
            raise ValueError("preserve request references absent raw message")
        # Carry explicit preservation forward even when an old summary was invalidated.
        summaries = self.store.list_records("summary", snapshot=snapshot)
        if summaries:
            preserve_ids |= set(summaries[-1].payload.get("preserve_refs", [])) & available
        return raw, [r.ref for r in raw if r.record_id in preserve_ids or r.ref in preserve_ids]

    def _publish(self, raw, preserved, snapshot, mode, *, items=None, generator=None, batches=None, limitations=None,
                 covered_refs=None, processed_segments=None, reused_summary=None):
        persona = self.store.get_record("persona", snapshot=snapshot)
        states = self.store.list_records("temporary_state", snapshot=snapshot)
        excerpts = []
        for record in raw:
            keep = record.ref in preserved
            text = record.body_markdown if keep else record.body_markdown[:160]
            excerpts.append({"ref": record.ref, "role": record.payload["role"], "created_at": record.created_at, "text": text,
                             "full_preserved": keep, "truncated": text != record.body_markdown})
        refs = [r.ref for r in raw]
        covered_refs = refs if covered_refs is None else covered_refs
        semantic = items is not None
        body = "\n".join(f"[{x['ref']}] {x['text']}" for x in excerpts)
        if semantic:
            body = "模型转述（非原话；细节以来源为准）：\n" + "\n".join(
                f"[{x['category']}] {x['text']} ({', '.join(x['source_refs'])})" for x in items)
        summary_id = generator["run_id"] if generator else uid("summary")
        summary = Record(record_id=summary_id, record_kind="summary", agent_id=self.store.agent_id,
                         user_id=self.store.user_id, author_kind="model" if generator else "system", generator=generator,
                         source_refs=[*refs, persona.ref, *[r.ref for r in states], *([reused_summary] if reused_summary else [])], body_markdown=body,
                         payload={"mode": mode, "source_range": covered_refs, "excerpts": excerpts,
                                  "persona_ref": persona.ref, "state_refs": [r.ref for r in states], "snapshot": snapshot,
                                  "preserve_refs": preserved, "source_watermark": covered_refs[-1] if covered_refs else None,
                                  "strategy": "llm-semantic-v1" if semantic else "source-excerpts-v1",
                                  "semantic_items": items or [], "llm_batches": batches or [],
                                  "processed_segments": processed_segments or [], "reused_summary_ref": reused_summary,
                                  "limitations": limitations or []})
        expected = {persona.record_id: persona.revision, **{r.record_id: r.revision for r in states}}
        self.store.commit([summary], idempotency_key=summary.record_id, expected=expected)
        return summary

    def compact(self, *, mode="manual", preserve_ids=None, snapshot=None, protocol_messages=None, limitations=None):
        """Explicit synchronous excerpt baseline, used by offline callers."""
        if protocol_messages is not None:
            validate_protocol(protocol_messages)
        snapshot = snapshot or self.store.snapshot()
        raw, preserved = self._prepare(snapshot, preserve_ids)
        if not raw:
            return None
        return self._publish(raw, preserved, snapshot, mode, limitations=limitations)

    def _chunks(self, raw, processed=None):
        chunks, batch = [], []
        offsets = {}
        for span in processed or []:
            offsets[span["ref"]] = max(offsets.get(span["ref"], 0), span["end"])
        for record in raw:
            # Split oversized messages into literal Unicode-safe segments. All
            # segments refer to the original message; never summarize summaries.
            start = offsets.get(record.ref, 0)
            while start < len(record.body_markdown):
                end, size = start, 0
                while end < len(record.body_markdown) and size + len(record.body_markdown[end].encode("utf-8")) <= self.chunk_bytes // 3:
                    size += len(record.body_markdown[end].encode("utf-8"))
                    end += 1
                segment = {"ref": record.ref, "role": record.payload["role"], "created_at": record.created_at, "start": start, "end": end,
                           "text": record.body_markdown[start:end]}
                if batch and len(json.dumps([*batch, segment], ensure_ascii=False).encode("utf-8")) > self.chunk_bytes:
                    chunks.append(batch)
                    batch = []
                batch.append(segment)
                start = end
        if batch:
            chunks.append(batch)
        return chunks

    def _reuse(self, previous, raw):
        """Reuse derived items unchanged, rechecking their immutable raw spans.

        Neither the old summary text nor its paraphrases become model input.
        The cursor records literal raw coverage, including a split long message.
        """
        by_ref = {r.ref: r for r in raw}
        processed = previous.payload.get("processed_segments") or [
            {"ref": ref, "start": 0, "end": len(by_ref[ref].body_markdown)}
            for ref in previous.payload["source_range"] if ref in by_ref]
        offsets = {}
        for span in processed:
            record = by_ref.get(span["ref"])
            if record is None or not 0 <= span["start"] <= span["end"] <= len(record.body_markdown):
                raise ValueError("invalid cached raw coverage")
            if span["start"] > offsets.get(span["ref"], 0):
                raise ValueError("noncontiguous cached raw coverage")
            offsets[span["ref"]] = max(offsets.get(span["ref"], 0), span["end"])
        items = []
        for item in previous.payload.get("semantic_items", []):
            spans = [SourceSpan.model_validate(s) for s in item["source_spans"]]
            if not spans or item["source_refs"] != list(dict.fromkeys(s.message_ref for s in spans)):
                raise ValueError("invalid cached semantic sources")
            for span in spans:
                record = by_ref.get(span.message_ref)
                if (record is None or span.end <= span.start or
                        record.body_markdown[span.start:span.end] != span.quote or
                        span.end > offsets.get(span.message_ref, 0)):
                    raise ValueError("cached semantic evidence no longer matches raw input")
            items.append(dict(item))
        # Compact the durable cursor to one contiguous interval per raw record.
        processed = [{"ref": r.ref, "start": 0, "end": offsets[r.ref]} for r in raw if r.ref in offsets]
        return items, processed

    async def acompact(self, *, mode="manual", preserve_ids=None, snapshot=None, protocol_messages=None):
        """LLM semantic extraction with locally verified literal evidence."""
        if protocol_messages is not None:
            validate_protocol(protocol_messages)
        async with self._lock:
            if self.before_compact:
                await self.before_compact()
                snapshot = self.store.snapshot()
            snapshot = snapshot or self.store.snapshot()
            raw, preserved = self._prepare(snapshot, preserve_ids)
            if not raw:
                return None
            previous = self.current(snapshot)
            if (previous and previous.payload.get("strategy") == "llm-semantic-v1" and
                    previous.payload["source_range"] == [r.ref for r in raw] and
                    set(preserved) <= set(previous.payload.get("preserve_refs", []))):
                return previous
            if not self.gateway or not self.llm_enabled:
                return self.compact(mode=mode, snapshot=snapshot, preserve_ids=preserved,
                                    limitations=["LLM semantic compaction disabled; source excerpts used"])
            semantic_previous = previous if previous and previous.payload.get("strategy") == "llm-semantic-v1" else None
            items, processed, batches = [], [], []
            retained = None
            limitations = []
            try:
                if semantic_previous:
                    items, processed = self._reuse(semantic_previous, raw)
                    if not semantic_previous.generator:
                        raise ValueError("cached semantic summary lacks generator")
                    retained = (list(items), list(processed))
                chunks = self._chunks(raw, processed)
                if len(chunks) > self.max_chunks:
                    limitations.append(f"incremental_compaction_pending: processing {self.max_chunks}/{len(chunks)} remaining batches")
                for chunk in chunks[:self.max_chunks]:
                    request = ModelRequest(role="compaction", purpose="context_compaction", prompt_version="compaction/" + self.prompt_version,
                        budget_tokens=max(32000, self.chunk_bytes + 24000), max_tokens=self.output_tokens, output_schema=self.schema,
                        messages=[{"role": "system", "content": load_prompt("compaction", self.prompt_version)},
                                  {"role": "user", "content": json.dumps(chunk, ensure_ascii=False)}])
                    response = await self.gateway.generate(request)
                    if response.tool_calls:
                        raise ValueError("compaction does not authorize tool calls")
                    data = json.loads(response.content)
                    jsonschema.validate(data, self.schema)
                    if data["decision"] == "defer":
                        raise ProviderError("semantic_compaction_deferred")
                    for item in data["items"]:
                        spans = []
                        for evidence in item["evidence"]:
                            segment = next((s for s in chunk if s["ref"] == evidence["ref"] and evidence["quote"] in s["text"]), None)
                            if segment is None:
                                raise ValueError("summary evidence outside input or not verbatim")
                            start = segment["start"] + segment["text"].index(evidence["quote"])
                            spans.append(SourceSpan(message_ref=evidence["ref"], start=start,
                                end=start + len(evidence["quote"]), quote=evidence["quote"]).model_dump())
                        items.append({"category": item["category"], "text": item["text"], "source_spans": spans,
                                      "source_refs": list(dict.fromkeys(s["message_ref"] for s in spans))})
                    batches.append({"trace_id": request.trace_id, "provider": response.provider, "model": response.model,
                                    "source_refs": list(dict.fromkeys(s["ref"] for s in chunk)), "decision": data["decision"]})
                    processed.extend({"ref": s["ref"], "start": s["start"], "end": s["end"]} for s in chunk)
                offsets = {}
                for span in processed:
                    offsets[span["ref"]] = max(offsets.get(span["ref"], 0), span["end"])
                processed = [{"ref": r.ref, "start": 0, "end": offsets[r.ref]} for r in raw if r.ref in offsets]
                covered = [r.ref for r in raw if offsets.get(r.ref, 0) == len(r.body_markdown)]
                items = list({digest(item): item for item in items}.values())
                if any(b["provider"] == "fixture" for b in batches):
                    limitations.append("Offline fixture output; real model semantic quality not verified")
                last = batches[-1] if batches else (semantic_previous.generator if semantic_previous else None)
                if last is None:
                    raise ValueError("no semantic material or reusable result")
                generator = {"provider": last["provider"], "model": last["model"],
                             "prompt_version": "compaction/" + self.prompt_version, "run_id": uid("summary")}
                return self._publish(raw, preserved, snapshot, mode, items=items, generator=generator,
                                     batches=batches, limitations=limitations, covered_refs=covered,
                                     processed_segments=processed, reused_summary=semantic_previous.ref if semantic_previous else None)
            except (ProviderError, TimeoutError, ValueError, KeyError, TypeError, jsonschema.ValidationError, ConflictError) as exc:
                # No partial semantic summary is published. A fresh excerpt
                # summary is explicitly labelled as fallback, not API success.
                current = self.current()
                if retained is not None and current and current.ref == semantic_previous.ref:
                    # Preserve already validated agreements after a failed delta.
                    # Newly arrived raw stays uncovered and remains in history.
                    old_items, old_processed = retained
                    generator = {**semantic_previous.generator, "run_id": uid("summary")}
                    return self._publish(raw, preserved, snapshot, mode, items=old_items, generator=generator,
                        covered_refs=semantic_previous.payload["source_range"], processed_segments=old_processed,
                        reused_summary=semantic_previous.ref,
                        limitations=["LLM semantic compaction fallback: " + type(exc).__name__,
                                     "previous_verified_items_retained; new raw remains uncovered"])
                return self.compact(mode=mode, preserve_ids=preserved,
                    limitations=["LLM semantic compaction fallback: " + type(exc).__name__])

    def current(self, snapshot=None):
        summaries = self.store.list_records("summary", snapshot=snapshot)
        if not summaries:
            return None
        summary = summaries[-1]
        if not self.summary_allowed(summary):
            return None
        persona = self.store.get_record("persona", snapshot=snapshot)
        if summary.payload["persona_ref"] != persona.ref:
            return None
        states = {r.ref for r in self.store.list_records("temporary_state", snapshot=snapshot)}
        if set(summary.payload["state_refs"]) != states:
            return None
        return summary
