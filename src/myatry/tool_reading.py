"""Model-facing projections only; authoritative retrieval and records stay intact."""
from __future__ import annotations

import json
import re

from myatri.persona.temporary import resolve
from myatri.conversation.serialization import compact_json


TOOL_BYTES = 6000
FIELDS = ("body_markdown", "payload_json", "source_refs_json", "metadata_json")
READING_RULES = (
    "原文读取规则：recall只返回搜索目录和摘录，不代表已读原文。需要细节时用read_record。"
    "read_progress只证明本轮工具实际返回的指定字段区间；next_offset为空仅表示当前页到末尾，"
    "不表示中间页已读。complete=false时按first_unread_offset补缺页，不能声称已读全文或全文没有某内容。"
    "已有足够片段证据即可据片段回答，无需强求读完。当前上下文已提供的原话也可作依据。"
    "失败读取不算读到；未找到确切原话要直说。没有实际后台任务不得说稍后会继续翻阅。"
)


def byte_size(value):
    # Same serialization as the outgoing tool message, including JSON escapes.
    return len(compact_json(value).encode("utf-8"))


def recall_directory(bundle, store):
    """Expose only selected records or explicit budget/earliest source pointers.

    Do not recover candidates from reasons/graph paths: those may precede the
    group subject filter. Resolve every locator against the retrieval snapshot.
    """
    records = {record.ref: record for record in bundle.records}
    pointers = []
    for note in bundle.limitations:
        if note.startswith("早期原文定位："):
            pointers.extend(re.findall(r"[A-Za-z0-9_-]{1,160}@[1-9][0-9]*", note.split("。", 1)[0]))
        else:
            match = re.match(r"回忆预算不足，候选 ([A-Za-z0-9_-]{1,160}@[1-9][0-9]*) 及其逐字原文组未注入", note)
            if match:
                pointers.append(match[1])
    # Earliest-source locators retain their priority even when their body did
    # not fit the internal recall budget.
    refs = list(dict.fromkeys([*pointers, *records]))
    result = {"results": [], "has_more": False, "omitted_count": 0,
              "note": "搜索摘录不是完整原文；用read_record(ref, offset=0)核对。"}
    for ref in refs[:5]:
        try:
            record = records.get(ref) or resolve(store, ref, bundle.snapshot)
        except (ValueError, KeyError):
            continue
        text = record.body_markdown or json.dumps(record.payload, ensure_ascii=False)
        row = {"ref": record.ref, "created_at": record.created_at,
               "record_kind": record.record_kind, "excerpt": text[:180],
               "excerpt_complete": len(text) <= 180,
               "original_read": False, "read_record": {"ref": record.ref, "offset": 0}}
        result["results"].append(row)
    # Drop diagnostics first, then excerpts, then lower ranked results. Always
    # preserve at least one exact source locator if a source exists.
    while byte_size(result) > TOOL_BYTES:
        if "note" in result:
            del result["note"]
        elif any(row.get("excerpt") for row in result["results"]):
            for row in result["results"]:
                row["excerpt"] = row.get("excerpt", "")[:len(row.get("excerpt", "")) // 2]
                row["excerpt_complete"] = False
        elif len(result["results"]) > 1:
            result["results"].pop()
        else:
            row = result["results"][0]
            result["results"] = [{"ref": row["ref"], "read_record": row["read_record"], "original_read": False}]
            break
    result["omitted_count"] = max(0, len(refs) - len(result["results"]))
    result["has_more"] = bool(result["omitted_count"])
    # Counts are included in the measured envelope.
    while byte_size(result) > TOOL_BYTES and len(result["results"]) > 1:
        result["results"].pop()
        result["omitted_count"] += 1
        result["has_more"] = True
    return result


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def read_audit(messages):
    """Reconstruct only our successful tool roundtrips, including crash retries."""
    calls, audit = {}, []
    for message in messages:
        for call in message.get("tool_calls") or []:
            calls[call["id"]] = call["function"]
        if message.get("role") != "tool":
            continue
        call = calls.get(message.get("tool_call_id"))
        if not call:
            continue
        try:
            result = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        try:
            args = json.loads(call["arguments"])
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        if not isinstance(result, dict):
            continue
        if call["name"] == "recall":
            audit.append({"tool": "recall", "success": "error" not in result,
                          "refs": [r["ref"] for r in result.get("results", [])],
                          "has_more": result.get("has_more", False), "error": result.get("error")})
        elif call["name"] in {"read_record", "read_group_message"}:
            progress = result.get("read_progress", {})
            success = "error" not in result and result.get("available", True) is not False
            ref = result.get("ref", args.get("ref"))
            message_id = args.get("message_id")
            field = result.get("content_field", args.get("field"))
            audit.append({"tool": call["name"], "ref": ref if isinstance(ref, str) and len(ref) <= 256 else None,
                          "message_id": message_id if isinstance(message_id, str) and len(message_id) <= 160 else None,
                          "success": success,
                          "field": field if isinstance(field, str) and field in (*FIELDS, "text") else None,
                          "range": result.get("returned_range") if success else None,
                          "total_chars": result.get("total_chars"),
                          "next_offset": result.get("next_offset"),
                          "complete": progress.get("complete", False),
                          "error": result.get("error") or result.get("reason")})
    return audit


def progress_for(audit, ref, field, start, end, total):
    previous = [row["range"] for row in audit if row.get("success") and (row.get("ref") or row.get("message_id")) == ref
                and row.get("field") == field and row.get("range")]
    ranges = merge_ranges([*previous, [start, end]])
    cursor = 0
    for left, right in ranges:
        if left > cursor:
            break
        cursor = max(cursor, right)
    return {"field": field, "covered_ranges": ranges[:12], "range_count": len(ranges),
            "ranges_omitted": max(0, len(ranges) - 12), "complete": cursor >= total,
            "first_unread_offset": cursor if cursor < total else None}


def read_page(store, ref, snapshot, audit, *, offset=0, limit=800, field=None):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 800:
        raise ValueError("invalid page")
    record = resolve(store, ref, snapshot)
    full = record.model_dump(mode="json")
    field = field or ("body_markdown" if record.body_markdown else "payload_json")
    if field not in FIELDS:
        raise ValueError("invalid field")
    material = {"body_markdown": record.body_markdown,
                "payload_json": json.dumps(record.payload, ensure_ascii=False, sort_keys=True),
                "source_refs_json": json.dumps(record.source_refs, ensure_ascii=False),
                "metadata_json": json.dumps({k: v for k, v in full.items()
                    if k not in {"body_markdown", "payload", "source_refs"}}, ensure_ascii=False, sort_keys=True)}
    text = material[field]
    if offset > len(text):
        raise ValueError("offset exceeds field length")
    chunk = text[offset:offset + limit]
    result = {"ref": record.ref, "record_kind": record.record_kind,
              "author_kind": record.author_kind, "created_at": record.created_at, "content_hash": record.content_hash,
              "content_field": field, "available_fields": list(FIELDS),
              "offset": offset, "total_chars": len(text),
              "source_refs": record.source_refs[:3], "source_ref_count": len(record.source_refs),
              "source_refs_complete": len(record.source_refs) <= 3,
              "note": "进度仅针对当前字段。到末页不等于读齐；摘录可作依据，缺页不能声称全文没有。"}
    identity = {}
    for key in ("role", "source_owner"):
        value = record.payload.get(key)
        if isinstance(value, str) and len(value) <= 160:
            identity[key] = value
    for key in ("channel_source", "group_attribution", "attribution"):
        value = record.payload.get(key)
        if isinstance(value, dict):
            identity[key] = {k: v for k, v in value.items() if k in {
                "sender_id", "sender_name", "message_id", "sent_at", "speaker_id",
                "subject_id", "attribution", "confirmed", "source_message_id"}
                and (v is None or isinstance(v, bool) or isinstance(v, str) and len(v) <= 160)}
    if identity:
        # This is explicitly a projection, not the full payload. Never truncate
        # an identifier into a different identity; payload_json remains exact.
        result["payload_context"] = identity
        result["payload_context_complete"] = False
    # Measure after progress is attached. Only bytes actually returned count
    # toward coverage; never use the requested limit as evidence of reading.
    while True:
        end = offset + len(chunk)
        result.update(content=chunk, returned_range=[offset, end],
                      next_offset=end if end < len(text) else None,
                      read_progress=progress_for(audit, ref, field, offset, end, len(text)))
        if byte_size(result) <= TOOL_BYTES:
            return result
        if "note" in result:
            del result["note"]
        elif result.get("source_refs"):
            result["source_refs"] = []
            result["source_refs_complete"] = not record.source_refs
        elif "payload_context" in result and byte_size({**result, "content": ""}) > TOOL_BYTES:
            del result["payload_context"]
        elif len(chunk) > 1:
            chunk = chunk[:max(1, len(chunk) // 2)]
        elif "payload_context" in result:
            del result["payload_context"]
        else:
            # Malformed/extreme metadata must not make an otherwise readable
            # source disappear. Full metadata remains accessible as a field.
            result.pop("created_at", None)
            result.pop("record_kind", None)
            if byte_size(result) > TOOL_BYTES:
                raise ValueError("page metadata exceeds budget")


def cold_page(result, message_id, audit):
    """Apply the same coverage semantics to the existing cold-original tool."""
    if not result.get("available"):
        return result
    result = {**result, "message": dict(result["message"])}
    start, total = result["offset"], result["total_chars"]
    if start > total:
        raise ValueError("offset exceeds field length")
    chunk = result["message"]["text"]
    while True:
        end = start + len(chunk)
        result["message"]["text"] = chunk
        result.update(content_field="text", returned_range=[start, end],
                      next_offset=end if end < total else None,
                      complete=start == 0 and end == total,
                      read_progress=progress_for(audit, message_id, "text", start, end, total))
        if byte_size(result) <= TOOL_BYTES:
            return result
        if len(result["message"]) > 1:
            result["message"] = {"text": chunk}
            result["metadata_omitted"] = True
        elif chunk:
            chunk = chunk[:len(chunk) // 2]
        else:
            raise ValueError("cold page metadata exceeds budget")


def guard_reading_claim(text, audit):
    latest = {}
    located = set()
    for row in audit:
        if row["tool"] == "recall" and row.get("success"):
            located.update(row.get("refs", []))
        if row["tool"] in {"read_record", "read_group_message"}:
            latest[(row.get("ref") or row.get("message_id"), row.get("field"))] = row
    incomplete = [row for row in latest.values() if not row["success"] or not row.get("complete")]
    located_only = located - {row.get("ref") for row in latest.values() if row.get("success")}
    incomplete.extend({"ref": ref, "success": False} for ref in located_only)
    # A complete read of one source does not certify a different, missing page.
    mentioned = [row for row in [*latest.values(), *({"ref": ref, "success": False}
                 for ref in located_only)] if row.get("ref") and row["ref"] in text]
    if mentioned:
        incomplete = [row for row in mentioned if not row["success"] or not row.get("complete")]
    full_claim = re.compile(r"(?:读|看|查|翻|核对|检索).{0,12}(?:全文|全部|完整|所有|读完|看完)|"
                           r"(?:全文|全部|所有).{0,14}(?:读|看|查|没有|未|不含)|"
                           r"(?:读完|看完|查完|翻完|核对完)|没有任何|"
                           r"read (?:the )?(?:whole|entire)|(?:whole|entire) (?:text|record)", re.I)
    claims_all = any(full_claim.search(clause) and not re.search(
        r"尚未|还没|未能|没(?:有)?(?:读|看|查|翻|核对)|不代表|"
        r"(?:目前|现在)(?:只|仅)|(?:所读|已读|这些|当前)片段|不能(?:据此)?(?:说|声称|确认)", clause)
        for clause in re.split(r"[，,。！？!?；;\n]", text))
    if incomplete and claims_all:
        if not any(row.get("success") for row in latest.values()):
            return "尚未成功读取完整原文，暂时不能确认确切原话。", "unread_source_claim"
        return "目前只读到了部分原文，尚未核对完整内容，不能据此说原文没有相关内容。", "incomplete_read_claim"
    if incomplete and not any(row.get("success") for row in latest.values()) and re.search(r"已经(?:读到|查到)|我(?:读到|查到)了", text):
        return "这次原文读取未成功，尚未查到确切原话。", "failed_read_claim"
    return text, None
