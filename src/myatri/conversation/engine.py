from __future__ import annotations

import asyncio
import json

import jsonschema

from myatri.contracts import ModelRequest
from myatri.providers.gateway import ProviderError, assistant_message, validate_protocol
from myatri.persona.temporary import resolve
from .context import ContextBuilder
from .window import context_stats
from .serialization import compact_json
from .tool_results import TOOL_RESULT_BYTES, encoded_size, recall_page, tool_audit


TOOLS = [{"type": "function", "function": {"name": "recall", "description": "搜索本地记忆，返回有界来源目录和短摘录；需原话时再read_record。next_offset非空时可用同query分页。",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 8}},
        "required": ["query"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "read_record", "description": "按精确ID@revision读取原始记录",
    "parameters": {"type": "object", "properties": {"ref": {"type": "string"},
        "offset": {"type": "integer", "minimum": 0},
        "field": {"type": "string", "enum": ["body_markdown", "payload_json", "source_refs_json"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 800}},
        "required": ["ref"], "additionalProperties": False}}}]


def read_record_page(store, ref, snapshot=None, *, offset=None, limit=None, field=None):
    """Keep small-record compatibility; page large text without dropping provenance."""
    if offset is not None and (type(offset) is not int or offset < 0):
        raise ValueError("offset must be a nonnegative integer")
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 800):
        raise ValueError("limit must be between 1 and 800")
    if field not in (None, "body_markdown", "payload_json", "source_refs_json"):
        raise ValueError("invalid record field")
    record = resolve(store, ref, snapshot)
    full = record.model_dump(mode="json")
    if offset is None and limit is None and field is None and encoded_size(full) <= TOOL_RESULT_BYTES:
        return full
    body = record.body_markdown
    content_field = "body_markdown"
    if field == "source_refs_json":
        body = json.dumps(record.source_refs, ensure_ascii=False)
        content_field = field
    elif field == "payload_json" or (not body and field != "body_markdown"):
        body = json.dumps(record.payload, ensure_ascii=False, sort_keys=True)
        content_field = "payload_json"
    offset, limit = offset or 0, limit or 800
    page = {"ref": record.ref, "record_kind": record.record_kind,
        "agent_id": record.agent_id[:96], "user_id": record.user_id[:96],
        "author_kind": record.author_kind, "created_at": record.created_at,
        "content_field": content_field, "offset": offset, "total_chars": len(body),
        "source_refs": record.source_refs[:3], "source_ref_count": len(record.source_refs),
        "source_refs_complete": len(record.source_refs) <= 3,
        "content_hash": record.content_hash}
    payload = {}
    for key in ("role", "reality_scope", "participants", "event_refs"):
        value = record.payload.get(key)
        if isinstance(value, str):
            payload[key] = value[:96]
        elif isinstance(value, list):
            payload[key] = [str(item)[:96] for item in value[:3]]
    for key in ("channel_source", "attribution"):
        value = record.payload.get(key)
        if isinstance(value, dict):
            payload[key] = {field: value[field][:96] if isinstance(value[field], str) else value[field]
                           for field in ("sender_id", "sender_name", "message_id", "speaker_id",
                            "subject_id", "attribution", "confirmed", "source_message_id")
                           if field in value and isinstance(value[field], (str, bool, type(None)))}
    if payload:
        page["payload"] = payload
    chunk = body[offset:offset + limit]
    while True:
        end = offset + len(chunk)
        page.update(body_markdown=chunk, next_offset=end if end < len(body) else None)
        if encoded_size(page) <= TOOL_RESULT_BYTES:
            return page
        if len(chunk) <= 1:
            # Optional attribution can be huge even when the body is short.
            page.pop("payload", None)
            continue
        chunk = chunk[:max(1, len(chunk) // 2)]


class Conversation:
    def __init__(self, store, gateway, recall, index=None, on_message=None, *, max_steps=4, enable_tools=True, protocol_store=None, context_options=None, on_answer=None):
        self.store, self.gateway, self.recall = store, gateway, recall
        self.context = ContextBuilder(store, recall, index, gateway=gateway, options=context_options)
        self.on_message = on_message
        # Synchronous durable-enqueue hook. It may run again after a crash or
        # stable-message retry, so the consumer owns its idempotency key.
        self.on_answer = on_answer
        self.max_steps, self.enable_tools = max_steps, enable_tools
        self.protocol_store = protocol_store
        self.tool_handlers = {}
        self.reply_guard = None
        self._lock = asyncio.Lock()
        if protocol_store:
            self.context.budget.anchor = protocol_store.get_meta("context_token_anchor")

    async def respond(self, text, *, message_id=None, metadata=None):
        async with self._lock:
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError("message metadata must be a dict")
            if metadata and set(metadata) & {"role", "content", "message_id"}:
                raise ValueError("message metadata cannot override the conversation identity")
            raw = self.store.append_message("user", text, message_id=message_id, **(metadata or {}))
            try:
                answer = self.store.get_record("answer_" + raw.record_id)
            except KeyError:
                pass
            else:
                if self.on_answer:
                    self.on_answer(answer)
                return answer
            # Queue ingestion before API wait; user text remains safe if the provider fails.
            if self.on_message:
                self.on_message(raw)
            tools = [*TOOLS, *[entry[0] for entry in self.tool_handlers.values()]] if self.enable_tools else []
            completed = sum(1 for r in self.store.list_records("raw_message")
                            if r.payload.get("role") == "assistant" and r.payload.get("reply_to"))
            checkpoint = (completed + 1) % self.context.trace_every_turns == 0
            saved = self.protocol_store.get_protocol(raw.record_id) if self.protocol_store else None
            if saved and saved.get("status") == "context_reset":
                raise ProviderError("turn belongs to a cleared context; use a new message ID")
            if saved and saved["status"] == "active":
                messages, snapshot, start_step = saved["messages"], saved["snapshot"], saved["steps"]
                audit = list(saved.get("tool_audit", []))
                stats = saved.get("context_stats") or context_stats(messages)
                validate_protocol(messages)
            else:
                messages, snapshot = await self.context.abuild_context(text, tools=tools, persist_activation=checkpoint)
                start_step = 0
                audit = []
                stats = self.context.explain_context().get("context_stats", {})
            for step in range(start_step, self.max_steps):
                reply_policy = self.context.reply_policy
                output_tokens = self.context.budget.output_reserve
                if reply_policy.maximum:
                    output_tokens = min(output_tokens, reply_policy.max_tokens)
                request = ModelRequest(messages=messages, tools=tools, role="foreground", purpose="conversation",
                                       budget_tokens=self.context.budget.limit, max_tokens=output_tokens,
                                       estimated_input_tokens=self.context.budget.estimate(messages, tools))
                self.context.budget.require(messages, tools)
                response = await self.gateway.generate(request)
                self.context.budget.calibrate(messages, response.usage, tools)
                if self.protocol_store and checkpoint:
                    self.protocol_store.set_meta("context_token_anchor", self.context.budget.anchor)
                if not response.tool_calls:
                    if self.reply_guard is not None:
                        checked = await self.reply_guard(response.content, raw, messages)
                        response = response.model_copy(update={"content": checked})
                    response = response.model_copy(update={"content": reply_policy.prepare(response.content)})
                    if not response.content:
                        raise ProviderError("empty reply after metadata removal; raw user message preserved")
                messages.append(assistant_message(response))
                if not response.tool_calls:
                    validate_protocol(messages)
                    answer = self.store.append_message("assistant", response.content,
                        message_id="answer_" + raw.record_id, reply_to=raw.ref,
                        provider=response.provider, model=response.model, context_snapshot=snapshot,
                        **({"quality_review": self.reply_guard.last_check} if self.reply_guard is not None
                           and hasattr(self.reply_guard, "last_check") else {}))
                    if self.protocol_store:
                        protocol = {"status": "completed", "snapshot": snapshot,
                            "steps": step+1, "reply_ref": answer.ref, "sent_at": raw.created_at,
                            "replied_at": answer.created_at, "full_checkpoint": checkpoint,
                            "context_stats": stats}
                        if audit:
                            protocol["tool_audit"] = audit
                        if checkpoint:
                            protocol["messages"] = messages
                            protocol["context"] = self.context.explain_context()
                        self.protocol_store.save_protocol(raw.record_id, protocol)
                    if checkpoint:
                        self.store.flush_projection()
                    if self.on_answer:
                        self.on_answer(answer)
                    return answer
                for call in response.tool_calls:
                    name = call.get("function", {}).get("name", "unknown")
                    try:
                        name = call["function"]["name"]
                        definition = next(t["function"] for t in tools if t["function"]["name"] == name)
                        args = json.loads(call["function"]["arguments"])
                        jsonschema.validate(args, definition["parameters"])
                        if name == "recall":
                            bundle = self.recall.recall(args["query"], snapshot=snapshot, budget=1800)
                            result = recall_page(bundle, offset=args.get("offset", 0), limit=args.get("limit", 4))
                        elif name in self.tool_handlers:
                            result = self.tool_handlers[name][1](**args)
                        else:
                            result = read_record_page(self.store, args["ref"], snapshot,
                                offset=args.get("offset"), limit=args.get("limit"), field=args.get("field"))
                        # Bounded result, without truncating JSON/protocol fields.
                        if encoded_size(result) > TOOL_RESULT_BYTES:
                            result = {"error": "record exceeds tool result budget", "ref": args.get("ref"), "suggestion": "use recall with narrower query"}
                    except Exception as exc:
                        result = {"error": type(exc).__name__, "message": "tool arguments or source invalid"}
                    audit.append({"step": step + 1, "call_id": call["id"], **tool_audit(name, result)})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": compact_json(result)})
                if self.protocol_store:
                    self.protocol_store.save_protocol(raw.record_id, {"status": "active", "snapshot": snapshot,
                        "steps": step+1, "messages": messages, "tool_audit": audit,
                        "context_stats": stats})
            raise ProviderError("tool step limit reached; raw user message preserved, no fictitious reply committed")
