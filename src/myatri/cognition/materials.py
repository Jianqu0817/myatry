"""Bounded, revision-pinned cognitive inputs. Selection is not new evidence."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from myatri.contracts import Record, digest, parse_ref


def json_bytes(value) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def prefix_bytes(text: str, limit: int) -> str:
    return text.encode("utf-8")[:max(0, limit)].decode("utf-8", errors="ignore")


def _terms(text):
    text = re.sub(r"\s+", "", text.lower())[:2000]
    return set(text[i:i + 2] for i in range(max(0, len(text) - 1)))


def _bound(value, string_bytes=500, items=6):
    if isinstance(value, str):
        return prefix_bytes(value, string_bytes)
    if isinstance(value, list):
        return [_bound(v, string_bytes, items) for v in value[:items]]
    if isinstance(value, dict):
        return {k: _bound(v, string_bytes, items) for k, v in list(value.items())[:24]}
    return value


def project_record(record, max_bytes=2800, *, complete=False):
    full = {"ref": record.ref, "record_id": record.record_id, "revision": record.revision,
            "record_kind": record.record_kind, "created_at": record.created_at, "text": record.body_markdown,
            "payload": record.payload, "source_refs": record.source_refs, "complete": True}
    if complete or json_bytes(full) <= max_bytes:
        return full
    # An exact prefix is an explicitly partial view, never a rewritten record.
    for limit in (600, 300, 120, 0):
        text = prefix_bytes(record.body_markdown, limit)
        row = {**full, "text": text, "payload": _bound(record.payload, limit, 4),
               "source_refs": record.source_refs[:6], "complete": False,
               "text_range": {"start": 0, "end": len(text), "total_chars": len(record.body_markdown)},
               "limitations": ["原文/结构字段仅为有界摘录；未展示部分不可推断，完整记录按ref保留。"],
               "omitted_source_refs": max(0, len(record.source_refs) - 6)}
        if json_bytes(row) <= max_bytes:
            return row
    return {"ref": record.ref, "record_kind": record.record_kind, "complete": False,
            "text": "", "limitations": ["记录仅保留版本指针，未展示正文。"]}


@dataclass
class MaterialSelection:
    snapshot: str | None
    sources: list[Record]
    material: list[dict]
    limitations: list[str]
    candidate_count: int
    omitted_count: int
    candidate_hash: str
    missing_required: list[str]

    def metadata(self):
        return {"selected_refs": [r.ref for r in self.sources], "candidate_count": self.candidate_count,
                "omitted_count": self.omitted_count, "candidate_hash": self.candidate_hash,
                "partial_refs": [m["ref"] for m in self.material if not m.get("complete")],
                "limitations": self.limitations, "missing_required": self.missing_required}


def select_materials(store, refs, *, snapshot=None, query="", required_refs=(), counter_refs=(),
                     max_bytes=14000, max_records=12, max_record_bytes=2800,
                     strict_required=False, include_counters=True) -> MaterialSelection:
    """Prefer required inputs, bounded counterevidence and relevant/recent material.

    ``refs`` must pin revisions. No latest-version substitution occurs. Required
    materials that cannot fit are reported; strict callers must defer rather
    than claim that a partial review covered all evidence.
    """
    snapshot = snapshot or store.snapshot()
    current = store.list_records(snapshot=snapshot)
    lookup = {r.ref: r for r in current}
    refs = list(dict.fromkeys([*required_refs, *refs, *counter_refs]))
    explicit = set(refs)
    counters = list(counter_refs)
    if include_counters:
        for record in current:
            if record.record_kind == "edge" and record.payload.get("type") == "contradicts":
                endpoints = [record.payload.get("from_ref"), record.payload.get("to_ref")]
                if any(ref in explicit for ref in endpoints):
                    counters.extend(ref for ref in endpoints if ref)
            elif record.ref in explicit and record.payload.get("role") == "counterexample_search":
                counters.extend(record.payload.get("counter_refs", []))
    refs = list(dict.fromkeys([*refs, *counters]))
    records = []
    for ref in refs:
        record_id, revision = parse_ref(ref)
        if ref not in lookup:
            lookup[ref] = store.get_record(record_id, revision=revision, snapshot=snapshot)
        records.append(lookup[ref])
    if not query:
        focus = sorted(records, key=lambda r: (r.created_at, r.record_id), reverse=True)
        query = " ".join(r.body_markdown[:600] for r in focus[:2])
    terms = _terms(query)
    important = {"persona_version": 5, "temporary_state": 4, "behavior_attempt": 3, "cognitive_run": 2}
    def ranking(record):
        overlap = len(terms & _terms(record.body_markdown)) / max(1, len(terms))
        return (overlap, important.get(record.record_kind, 0), record.created_at, record.record_id)
    required = list(dict.fromkeys(required_refs))
    # Reserve some space for contradictions before filling ordinary candidates.
    reserved_counters = sorted({ref for ref in counters if ref not in required},
                               key=lambda ref: ranking(lookup[ref]), reverse=True)[:max(1, max_records // 4)]
    ordered = list(dict.fromkeys([*required, *reserved_counters,
                                 *[r.ref for r in sorted(records, key=ranking, reverse=True)]]))
    selected, material = [], []
    for ref in ordered:
        if len(selected) >= max_records:
            break
        row = project_record(lookup[ref], max_record_bytes, complete=strict_required and ref in required)
        if json_bytes([*material, row]) > max_bytes:
            continue
        material.append(row)
        selected.append(lookup[ref])
    selected_refs = {r.ref for r in selected}
    missing = [ref for ref in required if ref not in selected_refs]
    limitations = ["材料按固定revision做相关性/近期性选择；未选中不表示不存在，不可由数量或检索相似推断人格成立。"]
    omitted = len(refs) - len(selected)
    if omitted:
        limitations.append(f"{len(refs)}个候选中{omitted}个未进入本次预算；本次结论只覆盖已展示材料。")
    if any(not row.get("complete") for row in material):
        limitations.append("部分记录为标明字符区间的原文摘录，不能把摘录当完整经历。")
    if missing:
        limitations.append("必需材料无法完整放入本次预算，应defer并缩小提案范围。")
    return MaterialSelection(snapshot, selected, material, limitations, len(refs), omitted, digest(refs), missing)
