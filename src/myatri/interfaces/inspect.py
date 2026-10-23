from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

from myatri.contracts import BehaviorAttempt, Record, TemporaryEntry, parse_ref
from myatri.storage import ArchiveStore
from myatri.security import is_private_path


def inspect_record(store, identifier):
    record_id, revision = parse_ref(identifier) if "@" in identifier else (identifier, None)
    record = store.get_record(record_id, revision)
    chain, seen = [], set()
    def visit(current):
        if current.ref in seen:
            return
        seen.add(current.ref)
        chain.append(current.model_dump(mode="json"))
        for ref in current.source_refs:
            rid, rev = parse_ref(ref)
            visit(store.get_record(rid, rev))
    visit(record)
    return {"record": record.model_dump(mode="json"), "source_chain": chain}


def doctor(app):
    app.store.flush_projection()
    issues = app.store.verify_archive()
    for state in app.states.list():
        if state.payload["status"] == "consolidated":
            try:
                ref = state.payload["persona_change_ref"]
                rid, rev = parse_ref(ref)
                if app.store.get_record(rid, rev).record_kind != "persona_change":
                    issues.append("invalid consolidation change ref")
            except (ValueError, KeyError):
                issues.append("missing consolidation source")
    provider = app.gateway.provider
    live_protocol_verified = getattr(provider, "name", "") == "deepseek" and getattr(provider, "capabilities_verified", False)
    return {"ok": not issues, "issues": issues, "snapshot": app.store.snapshot(),
            "index_generation": app.index.generation, "index_watermark": app.index.watermark,
            "jobs": len(app.queue.list_jobs()), "live_verified": bool(live_protocol_verified),
            "scheduler_issues": app.queue.get_meta("scheduler_issues", []),
            "failed_jobs": [{"id": job["id"], "role": job["role"], "error": job["error"]}
                            for job in app.queue.list_jobs() if job["status"] == "failed"],
            "live_verification_scope": "configured provider protocol capabilities, not longitudinal personality quality",
            "outreach_enabled": False,
            "emotion_enabled": app.emotions.config.enabled,
            "emotion_appraisals": len(app.store.list_records("emotion_appraisal")),
            "maintenance_turns": app.maintenance.turns,
            "deep_every_batches": app.maintenance.deep_every,
            "persona_every_batches": app.maintenance.persona_every,
            "trace_every_turns": app.conversation.context.trace_every_turns,
            "projection_every_commits": app.store.projection_every,
            "timezone": str(app.conversation.context.timezone),
            "last_maintenance": app.queue.get_meta("last_maintenance"),
            "context_limit": app.conversation.context.budget.limit,
            "compact_at": app.conversation.context.budget.compact_at,
            "embedding": "BGE local ONNX" if app.index.provider else "local TF-IDF lexical baseline"}


def import_record(store, path):
    record = Record.model_validate_json(Path(path).read_text(encoding="utf-8-sig"))
    if record.agent_id != store.agent_id or record.user_id != store.user_id:
        raise ValueError("import isolation mismatch")
    if record.author_kind != "human_import":
        raise ValueError("manual imports must explicitly identify human_import author")
    if record.record_kind == "temporary_state":
        entry = TemporaryEntry.model_validate(record.payload)
        if entry.status == "consolidated":
            raise ValueError("manual state import cannot bypass atomic persona consolidation")
    if record.record_kind == "behavior_attempt":
        attempt = BehaviorAttempt.model_validate(record.payload)
        if attempt.decision_status in {"expressed", "attempted"}:
            for ref in attempt.action_refs:
                rid, revision = parse_ref(ref)
                action = store.get_record(rid, revision)
                if action.record_kind != "raw_message" or action.payload.get("role") != "assistant":
                    raise ValueError("imported attempt lacks persisted assistant action")
    if record.record_kind == "persona_change" or (record.record_kind == "persona_version" and record.revision > 1):
        raise ValueError("persona revisions use persona patch/restore; importing a standalone version would omit its atomic change record")
    store.commit([record], idempotency_key="import-" + record.ref + "-" + record.content_hash,
                 expected={record.record_id: record.revision - 1})
    return record


def backup(app, output):
    output = Path(output).resolve()
    if output == app.root or app.root in output.parents:
        raise ValueError("backup output must be outside runtime root")
    if output.exists():
        raise ValueError("backup output exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="myatri-backup-") as temp:
        staged = Path(temp) / "data"
        with app.store._writer():
            generic_ignore = shutil.ignore_patterns("*.sqlite3", "*.sqlite3-*", ".writer.lock", "*.tmp", "*.part")
            def exclude_private(directory, names):
                ignored = set(generic_ignore(directory, names))
                for name in names:
                    path = Path(directory) / name
                    if (is_private_path(path.relative_to(app.root)) or path.is_symlink() or path.is_junction()):
                        ignored.add(name)
                return ignored
            shutil.copytree(app.root, staged, ignore=exclude_private)
            with closing(sqlite3.connect(app.queue.path)) as source, closing(sqlite3.connect(staged / "runtime.sqlite3")) as target:
                source.backup(target)
        metadata = {"schema_version": "1.0", "agent_id": app.store.agent_id, "user_id": app.store.user_id,
                    "note": "archive/index + SQLite runtime online backup; projection is rebuilt"}
        (staged / "backup.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
        with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
            for file in staged.rglob("*"):
                if file.is_file():
                    bundle.write(file, file.relative_to(staged).as_posix())
    return str(output)


def restore_backup(source, target):
    target = Path(target).resolve()
    if target.exists():
        raise ValueError("restore requires a new target directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="myatri-restore-", dir=target.parent) as temp:
        staged = Path(temp) / "data"
        staged.mkdir()
        with zipfile.ZipFile(source) as bundle:
            for item in bundle.infolist():
                resolved = (staged / item.filename).resolve()
                if is_private_path(item.filename):
                    raise ValueError("backup contains private credential/configuration paths")
                if staged.resolve() not in resolved.parents or "\\" in item.filename or item.file_size > 512 * 1024 * 1024:
                    raise ValueError("unsafe backup member")
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("symlink backup member")
            bundle.extractall(staged)
        metadata = json.loads((staged / "backup.json").read_text(encoding="utf-8"))
        store = ArchiveStore(staged, metadata["agent_id"], metadata["user_id"])
        issues = store.verify_archive()
        if issues:
            raise ValueError("backup archive integrity failed: " + str(issues))
        shutil.move(str(staged), str(target))
    return str(target)
