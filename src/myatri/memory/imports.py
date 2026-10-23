"""Import a frozen archive as attributed history, never as current group activity."""
from collections import Counter

from myatri.contracts import Record, digest


SEARCHABLE = {"raw_message", "event", "interpretation", "reflection", "summary", "temporary_state"}


def import_history(source, target, *, owner_label, snapshot=None):
    if source.domain_path.resolve() == target.domain_path.resolve():
        raise ValueError("source and target must be separate archives")
    if not owner_label.strip():
        raise ValueError("history owner must be explicit")
    snapshot = snapshot or source.snapshot()
    _, originals = source._load(snapshot)
    if not originals:
        raise ValueError("source archive is empty")
    namespace = digest([source.agent_id, source.user_id, snapshot, owner_label])[:24]
    marker_id = "private_import_" + namespace
    try:
        return target.get_record(marker_id).payload["report"]
    except KeyError:
        pass
    refs = {ref: "private_" + digest([namespace, ref])[:48] + "@1" for ref in originals}
    latest = {r.ref for r in source.list_records(snapshot=snapshot)}
    report = {"source_snapshot": snapshot, "source_agent": source.agent_id,
              "source_user": source.user_id, "owner_label": owner_label,
              "preserved_revisions": len(originals), "marker_ref": marker_id + "@1",
              "raw_messages": sum(r.record_kind == "raw_message" for r in originals.values()),
              "latest_kinds": dict(Counter(r.record_kind for r in source.list_records(snapshot=snapshot))),
              "original_to_import": refs, "model_api_calls": 0}
    marker = Record(record_id=marker_id, record_kind="cognitive_run",
                    agent_id=target.agent_id, user_id=target.user_id, author_kind="human_import",
                    body_markdown="用户授权将原私聊的冻结历史导入当前群。不是新发生的群聊，也不认证群成员身份。",
                    payload={"role": "human_history_import", "report": report})
    records = [marker]
    for ref, original in originals.items():
        searchable = ref in latest and original.record_kind in SEARCHABLE
        role = original.payload.get("role", original.record_kind)
        text = (f"[迁入的私聊历史；原对话人：{owner_label}；原类型：{original.record_kind}；"
                f"原角色：{role}；原时间：{original.created_at}；原引用：{ref}]\n" + original.body_markdown)
        records.append(Record(
            record_id=refs[ref].split("@")[0], record_kind="interpretation" if searchable else "cognitive_run",
            agent_id=target.agent_id, user_id=target.user_id, author_kind="human_import",
            created_at=original.created_at,
            source_refs=list(dict.fromkeys([marker.ref, *(refs[r] for r in original.source_refs)])),
            body_markdown=text,
            payload={"imported_private_memory": True, "source_owner": owner_label,
                     "source_snapshot": snapshot, "original_ref": ref,
                     "original": original.model_dump(mode="json"), "searchable_history": searchable,
                     "note": "历史材料保留原判断及其不确定性，不是当前人格、当前心情或新的外部证据。"}))
    target.commit(records, idempotency_key=marker_id,
                  expected={r.record_id: 0 for r in records})
    return report
