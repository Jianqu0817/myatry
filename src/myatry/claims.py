"""Read-only personal claims with explicit, evidence-linked change history."""
from datetime import datetime, timezone


def _instant(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else ""
    except (ValueError, TypeError, AttributeError):
        return ""


def claim_projection(records, subject):
    claims = []
    for record in records:
        if record.record_kind != "interpretation":
            continue
        item = record.payload.get("group_attribution") or record.payload.get("attribution", {})
        state = record.payload.get("claim_state", {})
        if (item.get("subject_id") != subject or item.get("attribution") != "self_report"
                or not state.get("facet")):
            continue
        claims.append({"ref": record.ref, "text": record.body_markdown, "facet": state["facet"],
            "source_message_ids": item.get("source_message_ids", []), "change": state.get("change", "assertion"),
            "supersedes_message_ids": state.get("supersedes_message_ids", []), "valid_time": state.get("valid_time"),
            "observed_at": state.get("observed_at", []), "status": "self_report_unverified",
            "recorded_at": record.created_at})
    claims.sort(key=lambda row: (max((_instant(value) for value in row["observed_at"]), default=""), row["recorded_at"], row["ref"]))
    for i, new in enumerate(claims):
        if new["change"] not in {"correction", "retraction"}:
            continue
        targets = set(new["supersedes_message_ids"])
        for old in claims[:i]:
            if old["facet"] == new["facet"] and targets & set(old["source_message_ids"]):
                old["status"] = "retracted" if new["change"] == "retraction" else "superseded"
                old["changed_by"] = new["ref"]
    active = [row for row in claims if row["status"] == "self_report_unverified"]
    for row in active:
        peers = [other for other in active if other["facet"] == row["facet"] and other["text"] != row["text"]]
        if peers:
            row["unresolved_peer_refs"] = [other["ref"] for other in peers]
    return {"subject_id": subject, "active": active[-20:], "history": claims[-40:],
            "note": "自述与解释，不是心理诊断；时间未明仍未知。只有同主体、同属性、明确来源链接的更正才替代旧理解。"}
