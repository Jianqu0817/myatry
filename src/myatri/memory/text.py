"""Small, explicit business text for indexing and lexical recall.

Versioned change records keep rationale in the body and edited wording in the
structured diff. Both are searchable; source references and audit payloads are
not indiscriminately serialized into semantic text.
"""
from myatri.contracts import Record


def record_search_text(record: Record) -> str:
    parts = [record.body_markdown, str(record.payload.get("fact_summary") or ""), str(record.payload.get("text") or "")]
    if record.record_kind == "persona_change":
        patch = record.payload.get("patch", {})
        parts.extend(str(patch.get(key) or "") for key in ("entry_id", "replacement", "rationale", "scope"))
        for entry_id, difference in record.payload.get("diff", {}).items():
            parts.append(str(entry_id))
            if isinstance(difference, dict):
                parts.extend(str(difference.get(key) or "") for key in ("before", "after"))
    return " ".join(part for part in parts if part)
