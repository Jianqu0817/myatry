"""Small model-facing views of immutable retrieval results, not new evidence."""
from __future__ import annotations

import re

from .serialization import compact_json


TOOL_RESULT_BYTES = 6000
_REF = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{1,160}@[1-9][0-9]*(?![0-9])")


def encoded_size(value):
    return len(compact_json(value).encode("utf-8"))


def record_preview(record, *, excerpt_chars=180):
    """Keep exact outer refs and a bounded description of explicit import wrappers."""
    entry = {"ref": record.ref, "record_kind": record.record_kind,
        "created_at": record.created_at, "author_kind": record.author_kind,
        "excerpt": record.body_markdown[:excerpt_chars], "total_chars": len(record.body_markdown),
        "excerpt_complete": len(record.body_markdown) <= excerpt_chars,
        "content_hash": record.content_hash, "source_refs": record.source_refs[:3],
        "source_ref_count": len(record.source_refs), "source_refs_complete": len(record.source_refs) <= 3}
    payload = record.payload
    if isinstance(payload.get("role"), str):
        entry["role"] = payload["role"][:80]
    channel = payload.get("channel_source")
    if isinstance(channel, dict):
        entry["channel_source"] = {key: channel[key][:96] for key in
            ("sender_id", "sender_name", "message_id") if isinstance(channel.get(key), str)}
    provenance = []
    current = record.model_dump(mode="json")
    for _ in range(8):
        data = current.get("payload", {})
        if current.get("author_kind") != "human_import" or not data.get("imported_private_memory"):
            break
        original = data.get("original")
        if not isinstance(original, dict):
            break
        provenance.append({key: str(value)[:180] for key, value in {
            "original_ref": data.get("original_ref", ""),
            "created_at": original.get("created_at", ""),
            "record_kind": original.get("record_kind", ""),
            "role": original.get("payload", {}).get("role", "")}.items()})
        current = original
    if provenance:
        entry["import_provenance"] = provenance[:3]
        entry["import_depth_inspected"] = len(provenance)
        entry["import_provenance_complete"] = len(provenance) <= 3
    return entry


def recall_page(bundle, *, offset=0, limit=4):
    """Project only already selected material; never rerank or bypass identity filters.

    Budget omissions already expose version pointers in the service's limitations.
    Keep those as unread locators, without resolving or disclosing their contents.
    """
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8:
        raise ValueError("invalid recall page")
    entries = [record_preview(record) for record in bundle.records]
    known = {record.ref for record in bundle.records}
    for limitation in bundle.limitations:
        for ref in _REF.findall(limitation):
            if ref not in known:
                entries.append({"ref": ref, "content_loaded": False,
                    "note": "仅来源定位，尚未读取正文；使用 read_record 核对。"})
                known.add(ref)
    page = {"snapshot": bundle.snapshot, "index_watermark": bundle.index_watermark,
        "records": [], "offset": offset, "total_records": len(entries), "next_offset": None,
        "limitations": [text[:180] for text in bundle.limitations[:3]],
        "limitation_count": len(bundle.limitations), "limitations_complete": len(bundle.limitations) <= 3
            and all(len(text) <= 180 for text in bundle.limitations[:3]),
        "reading": "摘录和检索维护信息不是完整原文。用 read_record(ref, offset, limit) 读取；"
            "本目录有 next_offset 时，用相同 query 和该 offset 继续 recall。"
            "嵌套迁入信息可用 read_record 的 field=payload_json 分页核对。"}
    for entry in entries[offset:offset + limit]:
        page["records"].append(entry)
        page["next_offset"] = offset + len(page["records"]) if offset + len(page["records"]) < len(entries) else None
        if encoded_size(page) > TOOL_RESULT_BYTES:
            page["records"].pop()
            break
    # A single unusually deep import still has a readable exact version pointer.
    if not page["records"] and offset < len(entries):
        entry = entries[offset]
        page["records"] = [{"ref": entry["ref"], "content_loaded": False,
            "note": "元数据较长，使用 read_record 分页读取。"}]
    end = offset + len(page["records"])
    page["next_offset"] = end if end < len(entries) else None
    return page


def tool_audit(name, result):
    """Retain just tool outcomes in completed protocols, without full prompt storage."""
    refs = []
    if isinstance(result, dict):
        if isinstance(result.get("ref"), str):
            refs.append(result["ref"])
        refs.extend(row["ref"] for row in result.get("records", [])
                    if isinstance(row, dict) and isinstance(row.get("ref"), str))
    refs = list(dict.fromkeys(refs))
    rows = result.get("records", []) if isinstance(result, dict) else []
    pending = isinstance(result, dict) and (result.get("next_offset") is not None
        or any(isinstance(row, dict) and (row.get("excerpt_complete") is False
            or row.get("content_loaded") is False or row.get("source_refs_complete") is False)
            for row in rows))
    error = result.get("error") if isinstance(result, dict) else None
    return {"name": str(name)[:80], "success": not bool(error), "source_refs": refs[:20],
        "source_ref_count": len(refs), "result_bytes": encoded_size(result),
        "pending_pagination": bool(pending), **({"error_type": str(error)[:100]} if error else {})}
