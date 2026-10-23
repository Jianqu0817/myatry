"""Transport-observed group identities; aliases never authenticate an account.

This projection is rebuildable. It does not rewrite immutable conversation records
and makes no model calls. Only caller-supplied owner configuration is authoritative.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


def _mapping(record: Any) -> dict:
    if isinstance(record, Mapping):
        result = dict(record)
        body = result.get("body_markdown")
    else:
        result = {key: getattr(record, key) for key in ("id", "ref", "payload")
                  if hasattr(record, key)}
        body = getattr(record, "body_markdown", None)
    payload = result.get("payload")
    archive_role = payload.get("role") if isinstance(payload, Mapping) else None
    if isinstance(body, str):
        try:
            decoded = json.loads(body)
        except (ValueError, TypeError):
            return result
        if isinstance(decoded, dict):
            result = {**result, **decoded}
    if archive_role is not None:
        result["_archive_role"] = archive_role
    return result


def _messages(record: Any, source_id: str = "", _depth: int = 0) -> Iterable[dict]:
    if _depth > 8:
        return
    root = _mapping(record)
    if root.get("_archive_role", "user") != "user":
        return
    source = source_id or str(root.get("ref") or root.get("id") or "")
    root["archive_source_id"] = source
    if "sender_id" in root:
        yield root
    recent = root.get("recent_messages", [])
    if isinstance(recent, list):
        for item in recent:
            if isinstance(item, Mapping):
                yield from _messages(item, source, _depth + 1)


def _time(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else ""


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.lower() not in {
        "none", "null", "unknown", "0"
    } and not any(c in value for c in "\r\n|")


def _human(message: Mapping) -> bool:
    # bot_id may also be the configured owner's login; bot text is never a human
    # self-report. Handwritten messages from that shared login need a separate path.
    return (message.get("role", "user") == "user"
            and message.get("sender_id") != message.get("bot_id"))


class IdentityRegistry:
    def __init__(self, path, *, owner_id: str = "", owner_name: str = ""):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if owner_id and not _valid_id(owner_id):
            raise ValueError("invalid owner_id")
        self.owner_id, self.owner_name = owner_id, owner_name
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS members (
                    scope TEXT NOT NULL, member_id TEXT NOT NULL,
                    display_name TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(scope, member_id));
                CREATE TABLE IF NOT EXISTS aliases (
                    scope TEXT NOT NULL, member_id TEXT NOT NULL,
                    name TEXT NOT NULL, source_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, archive_source_id TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(scope, member_id, name, source_id));
                CREATE TABLE IF NOT EXISTS legacy_candidates (
                    scope TEXT NOT NULL, record_ref TEXT NOT NULL, name TEXT NOT NULL,
                    member_id TEXT NOT NULL, status TEXT NOT NULL, source_ids TEXT NOT NULL,
                    confirmed INTEGER NOT NULL DEFAULT 0 CHECK(confirmed = 0),
                    PRIMARY KEY(scope,record_ref,name,member_id));
            """)
            if "archive_source_id" not in {row[1] for row in db.execute("PRAGMA table_info(aliases)")}:
                db.execute("ALTER TABLE aliases ADD COLUMN archive_source_id TEXT NOT NULL DEFAULT ''")
            db.execute("CREATE INDEX IF NOT EXISTS aliases_recent ON aliases(scope,member_id,observed_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS aliases_names ON aliases(scope,name,member_id)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def observe(self, scope: str, message: Mapping, *, source_id: str = "") -> bool:
        """Observe channel metadata, ignoring text claims and assistant output."""
        with closing(self._connect()) as db, db:
            return self._observe(db, scope, message, source_id=source_id)

    @staticmethod
    def _observe(db, scope: str, message: Mapping, *, source_id: str = "") -> bool:
        """Apply one observation on the caller's transaction, including backfill."""
        member_id = message.get("sender_id")
        if not _valid_id(member_id) or not _human(message):
            return False
        name = message.get("sender_name", "")
        name = name.strip() if isinstance(name, str) else ""
        timestamp = _time(message.get("sent_at"))
        source = str(message.get("message_id") or source_id or "")
        archive_source = str(source_id or message.get("archive_source_id") or "")
        db.execute("""INSERT INTO members VALUES (?,?,?,?)
                ON CONFLICT(scope,member_id) DO UPDATE SET
                    display_name=CASE WHEN excluded.last_seen >= members.last_seen
                        AND excluded.display_name != '' THEN excluded.display_name
                        ELSE members.display_name END,
                    last_seen=MAX(members.last_seen,excluded.last_seen)
                WHERE excluded.last_seen > members.last_seen
                    OR (excluded.last_seen >= members.last_seen AND excluded.display_name != ''
                        AND excluded.display_name != members.display_name)""",
                       (scope, member_id, name, timestamp))
        if name:
            db.execute("""INSERT INTO aliases VALUES (?,?,?,?,?,?)
                    ON CONFLICT(scope,member_id,name,source_id) DO UPDATE SET
                    archive_source_id=CASE WHEN excluded.archive_source_id != ''
                        THEN excluded.archive_source_id ELSE aliases.archive_source_id END
                WHERE excluded.archive_source_id != ''
                    AND excluded.archive_source_id != aliases.archive_source_id""",
                           (scope, member_id, name, source, timestamp, archive_source))
        return True

    def backfill(self, scope: str, records: Iterable[Any]) -> dict:
        """Read old envelopes only; do not infer IDs from names in plain text."""
        records_seen = observations = ignored = 0
        # Old envelopes can repeat the same recent history hundreds of times.
        # One connection and commit avoids per-observation disk synchronization
        # during application initialization. Preserve the original observation order.
        with closing(self._connect()) as db, db:
            for record in records:
                records_seen += 1
                for message in _messages(record):
                    if (isinstance(message.get("sender_name"), str) and message["sender_name"].strip()
                            and self._observe(db, scope, message, source_id=message["archive_source_id"])):
                        observations += 1
                    else:
                        ignored += 1
            members = db.execute("SELECT COUNT(*) FROM members WHERE scope=?", (scope,)).fetchone()[0]
            aliases = db.execute("SELECT COUNT(*) FROM aliases WHERE scope=?", (scope,)).fetchone()[0]
        return {"records_seen": records_seen, "observations": observations,
                "ignored": ignored, "members": members, "aliases": aliases,
                "immutable_records_modified": 0}

    def resolve_name(self, scope: str, name: str) -> dict:
        """Unique is a local alias lookup, not proof that an old claim is theirs."""
        with closing(self._connect()) as db:
            candidates = [row[0] for row in db.execute(
                "SELECT DISTINCT member_id FROM aliases WHERE scope=? AND name=? ORDER BY member_id",
                (scope, name))]
        if self.owner_id and name == self.owner_name and self.owner_id not in candidates:
            candidates.append(self.owner_id)
        status = "unique" if len(candidates) == 1 else "ambiguous" if candidates else "unconfirmed"
        return {"status": status, "member_id": candidates[0] if status == "unique" else None,
                "name": name, "candidates": sorted(candidates),
                "manual_binding": status == "unique" and candidates[0] == self.owner_id}

    def resolve_legacy(self, scope: str, name: str, source_ids: Iterable[str]) -> dict:
        """Candidate only when this name was observed on the same source message.

        Even a globally unique name cannot transfer an old private-chat memory.
        Callers retain the original attribution and confirmation state.
        """
        sources = {str(source) for source in source_ids}
        with closing(self._connect()) as db:
            rows = db.execute("SELECT member_id,source_id,archive_source_id FROM aliases WHERE scope=? AND name=?",
                              (scope, name)).fetchall()
        candidates = sorted({row[0] for row in rows if row[1] in sources or row[2] in sources})
        status = "candidate" if len(candidates) == 1 else "ambiguous" if candidates else "unconfirmed"
        return {"status": status, "member_id": candidates[0] if status == "candidate" else None,
                "candidates": candidates, "confirmed": False}

    def record_legacy(self, scope: str, record_ref: str, name: str, source_ids: Iterable[str]) -> dict:
        """Project source-supported legacy candidates; never merge or authenticate.

        Rerunning replaces only this record/name's projection. Unknown names are
        not persisted, and any obsolete candidates for that pair are removed.
        """
        sources = sorted({str(source) for source in source_ids if source})
        resolution = self.resolve_legacy(scope, name, sources)
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM legacy_candidates WHERE scope=? AND record_ref=? AND name=?",
                       (scope, record_ref, name))
            if resolution["status"] in {"candidate", "ambiguous"}:
                for member_id in resolution["candidates"]:
                    db.execute("INSERT INTO legacy_candidates VALUES (?,?,?,?,?,?,0)",
                               (scope, record_ref, name, member_id, resolution["status"],
                                json.dumps(sources, ensure_ascii=False)))
        return {**resolution, "record_ref": record_ref, "name": name,
                "saved": len(resolution["candidates"])}

    def context(self, scope: str, current_id: str, relevant_ids: Iterable[str] = ()) -> dict:
        with closing(self._connect()) as db:
            rows = db.execute("SELECT * FROM members WHERE scope=? ORDER BY member_id", (scope,)).fetchall()
            aliases = db.execute("SELECT member_id,name FROM aliases WHERE scope=? GROUP BY member_id,name", (scope,)).fetchall()
        lookup = {row["member_id"]: dict(row) for row in rows}
        if self.owner_id and self.owner_id not in lookup:
            lookup[self.owner_id] = {"member_id": self.owner_id, "display_name": self.owner_name,
                                     "last_seen": "", "scope": scope}
        if current_id not in lookup:
            lookup[current_id] = {"member_id": current_id, "display_name": "", "last_seen": "", "scope": scope}
        selected = {current_id, *relevant_ids}
        if self.owner_id:
            selected.add(self.owner_id)
        with closing(self._connect()) as db:
            legacy = [dict(row) for row in db.execute(
                "SELECT record_ref,name,member_id,status,source_ids,confirmed "
                "FROM legacy_candidates WHERE scope=? ORDER BY record_ref,name,member_id", (scope,))
                if row["member_id"] in selected]
        for candidate in legacy:
            candidate["source_ids"] = json.loads(candidate["source_ids"])
            candidate["confirmed"] = False
        members = []
        for member_id in sorted(selected):
            if member_id not in lookup:
                continue
            row = lookup[member_id]
            row["aliases"] = sorted({a["name"] for a in aliases if a["member_id"] == member_id})
            with closing(self._connect()) as db:
                row["source_message_ids"] = sorted({a[0] for a in db.execute(
                    "SELECT source_id FROM aliases WHERE scope=? AND member_id=? AND source_id!='' "
                    "ORDER BY observed_at DESC,rowid DESC LIMIT 5", (scope, member_id))})
            row["manual_binding"] = member_id == self.owner_id
            row["is_owner"] = member_id == self.owner_id
            # Explicit owner name wins over a bot nickname on the shared login.
            if row["is_owner"] and self.owner_name:
                row["display_name"] = self.owner_name
            members.append(row)
        return {"current_member": next(row for row in members if row["member_id"] == current_id),
                "members": members, "owner": {"member_id": self.owner_id or None,
                "display_name": self.owner_name, "verified": bool(self.owner_id),
                "verification": "manual_config" if self.owner_id else "unbound"},
                "inherited_private_memory_owner_id": self.owner_id or None,
                "legacy_candidates": legacy,
                "legacy_candidate_rule": "旧资料中的姓名与成员ID仅为同来源候选，非身份认证；保留未确认，不能据此转移原私聊记忆或合并不同成员。",
                "name_is_authentication": False}

    def validate_memory(self, scope: str, item: Mapping, evidence: Iterable[Any]) -> dict:
        """Fail closed on cross-person self-reports and uncited identity claims.

        item: speaker_id, subject_id, attribution=self_report|reported, evidence
        [{message_id, quote}]. Evidence must be actual channel envelopes, not the
        summarizer's generated prose. Third-person reports remain unconfirmed.
        """
        del scope  # Attribution comes from cited messages, never alias similarity.
        speaker, subject = item.get("speaker_id"), item.get("subject_id")
        attribution = item.get("attribution", item.get("statement_type"))
        refs = item.get("evidence", [])

        def reject(reason):
            return {"valid": False, "reason": reason, "normalized": None}

        if not _valid_id(speaker) or not _valid_id(subject):
            return reject("missing_identity")
        if attribution not in {"self_report", "reported"}:
            return reject("missing_attribution")
        if attribution == "self_report" and speaker != subject:
            return reject("self_report_subject_mismatch")
        if not isinstance(refs, list) or not refs:
            return reject("missing_evidence")
        messages = [message for record in evidence for message in _messages(record)
                    if _human(message)]
        for ref in refs:
            if not isinstance(ref, Mapping) or not isinstance(ref.get("quote"), str) or not ref["quote"].strip():
                return reject("missing_exact_quote")
            source = ref.get("message_id", ref.get("source_id"))
            matches = [message for message in messages
                       if source and source in {message.get("message_id"), message.get("archive_source_id")}
                       and ref["quote"] in message.get("text", "")]
            if not matches:
                return reject("quote_not_found")
            if {message["sender_id"] for message in matches} != {speaker}:
                return reject("evidence_speaker_mismatch")
        normalized = dict(item)
        normalized["attribution"] = attribution
        normalized["source_speaker_ids"] = [speaker]
        normalized["confirmed"] = attribution == "self_report"
        if attribution == "reported":
            normalized["confirmation"] = "third_party_report_only"
        return {"valid": True, "reason": "", "normalized": normalized}
