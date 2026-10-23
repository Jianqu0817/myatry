"""Manifest-last publication with one cross-process writer.

Only files referenced by published manifests are visible. A crash before the
manifest leaves harmless orphan files; a crash after publication is replayed
into the disposable projection. All validation reads the immutable authority.
"""

from __future__ import annotations

import hashlib
import copy
from collections import OrderedDict
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from typing import Iterator
import uuid

from myatri.contracts import AUTHOR_KINDS, Event, Record, SourceSpan
from myatri._vendor.letta import atomic_write


class ArchiveError(ValueError):
    pass


class ConflictError(ArchiveError):
    pass


class IntegrityError(ArchiveError):
    pass


class IdempotencyError(ConflictError):
    pass


_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCK_GUARD = threading.Lock()
_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,199}$")
_IMMUTABLE_KINDS = {"message", "raw_message", "event", "raw_event", "experience", "emotion_appraisal", "emotion_retraction"}


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_id(value: str) -> None:
    if not _ID.fullmatch(value) or ".." in value:
        raise ArchiveError(f"Unsafe record identifier: {value!r}")


def _atomic_write(path: Path, data: bytes) -> None:
    # Keep the existing ArchiveStore/MemoryIndex seam and failure-injection API.
    atomic_write(path, data)


class ArchiveStore:
    def __init__(self, root: Path, agent_id: str = "atri", user_id: str = "local", *, projection_every: int = 1):
        if type(projection_every) is not int or projection_every < 1:
            raise ValueError("projection_every must be a positive integer")
        self.projection_every = projection_every
        self._projection_pending = 0
        self.root = Path(root).resolve()
        self.agent_id, self.user_id = agent_id, user_id
        self.root.mkdir(parents=True, exist_ok=True)
        domain = _hash(_json([agent_id, user_id]))
        self.domain_path = self.root / "domains" / domain
        self.manifest_path = self.domain_path / "commits"
        self.manifest_path.mkdir(parents=True, exist_ok=True)
        self.db_path = self.domain_path / "projection.sqlite3"
        with _LOCK_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(str(self.root), threading.RLock())
        self.recover()

    @contextmanager
    def _writer(self) -> Iterator[None]:
        with self._thread_lock:
            with (self.root / ".writer.lock").open("a+b") as stream:
                stream.seek(0, 2)
                if not stream.tell():
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    # LK_LOCK has a fixed retry budget; use a controlled blocking loop.
                    while True:
                        try:
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                            break
                        except OSError:
                            time.sleep(0.02)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _fingerprint(path):
        stat = path.stat()
        return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns

    def _document(self, path, kind, expected_hash=None):
        signature = self._fingerprint(path)
        cached = self._file_cache.get(path)
        if cached is None or cached[0] != signature:
            data = path.read_bytes()
            if expected_hash is not None and _hash(data) != expected_hash:
                raise IntegrityError(f"Record checksum mismatch: {path.name}")
            value = Record.model_validate_json(data) if kind == "record" else json.loads(data)
            cached = signature, _hash(data), value
            self._file_cache[path] = cached
        return cached

    def _load(self, snapshot: str | None = None, *, force=False) -> tuple[list[dict], dict[str, Record]]:
        # Returned models contain mutable nested payloads. Never expose cache
        # objects to callers. File metadata invalidates ordinary reads; doctor
        # explicitly rehashes every committed byte, even if metadata was forged.
        with self._thread_lock:
            if not hasattr(self, "_file_cache") or force:
                self._file_cache, self._load_cache = {}, OrderedDict()
            paths = sorted(self.manifest_path.glob("*.json"))
            cached = self._load_cache.get(snapshot)
            if cached:
                names, fingerprints, result = cached
                visible = paths if snapshot is None else paths[:len(names)]
                try:
                    unchanged = visible == names and all(self._fingerprint(path) == stamp for path, stamp in fingerprints.items())
                except OSError:
                    unchanged = False
                if unchanged:
                    self._load_cache.move_to_end(snapshot)
                    return copy.deepcopy(result)
            result = self._load_validated(snapshot, paths)
            manifests, _ = result
            names = paths[:len(manifests)]
            used = list(names)
            used.extend((self.domain_path / entry["path"]).resolve() for manifest in manifests for entry in manifest["records"])
            fingerprints = {path: self._file_cache[path][0] for path in used}
            self._load_cache[snapshot] = names, fingerprints, result
            self._load_cache.move_to_end(snapshot)
            while len(self._load_cache) > 4:
                self._load_cache.popitem(last=False)
            return copy.deepcopy(result)

    def _load_validated(self, snapshot, paths) -> tuple[list[dict], dict[str, Record]]:
        manifests: list[dict] = []
        records: dict[str, Record] = {}
        previous = None
        found_snapshot = snapshot is None
        for index, path in enumerate(paths, 1):
            try:
                manifest = copy.deepcopy(self._document(path, "manifest")[2])
                digest = manifest.pop("manifest_sha256")
                if digest != _hash(_json(manifest)):
                    raise IntegrityError(f"Manifest checksum mismatch: {path.name}")
                manifest["manifest_sha256"] = digest
                if manifest["sequence"] != index or manifest["previous_commit"] != previous:
                    raise IntegrityError("Manifest sequence or parent mismatch")
                if manifest["agent_id"] != self.agent_id or manifest["user_id"] != self.user_id:
                    raise IntegrityError("Manifest isolation mismatch")
                for entry in manifest["records"]:
                    relative = Path(entry["path"])
                    target = (self.domain_path / relative).resolve()
                    if relative.is_absolute() or not target.is_relative_to(self.domain_path.resolve()):
                        raise IntegrityError("Manifest path escapes isolated archive")
                    _, sha256, record = self._document(target, "record", entry["sha256"])
                    if sha256 != entry["sha256"]:
                        raise IntegrityError(f"Record checksum mismatch: {entry['ref']}")
                    if record.ref != entry["ref"] or record.agent_id != self.agent_id or record.user_id != self.user_id:
                        raise IntegrityError("Record identity or isolation mismatch")
                    if record.ref in records:
                        raise IntegrityError(f"Duplicate immutable revision: {record.ref}")
                    records[record.ref] = record
                manifests.append(manifest)
                previous = manifest["commit_id"]
                if snapshot == previous:
                    found_snapshot = True
                    break
            except IntegrityError:
                raise
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise IntegrityError(f"Cannot read committed archive {path.name}: {error}") from error
        if not found_snapshot:
            raise ArchiveError(f"Unknown snapshot in current isolation domain: {snapshot}")
        return manifests, records

    def snapshot(self) -> str | None:
        manifests, _ = self._load()
        return manifests[-1]["commit_id"] if manifests else None

    def get_record(self, record_id: str, revision: int | None = None, snapshot: str | None = None) -> Record:
        _safe_id(record_id)
        _, records = self._load(snapshot)
        candidates = [r for r in records.values() if r.record_id == record_id and (revision is None or r.revision == revision)]
        if not candidates:
            raise KeyError(f"Record unavailable in current domain/snapshot: {record_id}@{revision}")
        return max(candidates, key=lambda r: r.revision)

    def list_records(self, kind: str | None = None, snapshot: str | None = None) -> list[Record]:
        _, records = self._load(snapshot)
        latest: dict[str, Record] = {}
        for record in records.values():
            if record.record_id not in latest or record.revision > latest[record.record_id].revision:
                latest[record.record_id] = record
        return sorted((r for r in latest.values() if kind is None or r.record_kind == kind), key=lambda r: (r.created_at, r.record_id))

    def commit(self, records: list[Record], idempotency_key: str, expected: dict[str, int] | None = None, fail_at: str | None = None) -> str:
        if not records or not idempotency_key:
            raise ArchiveError("A nonempty atomic batch and idempotency key are required")
        if fail_at not in {None, "after_files", "after_manifest", "after_projection"}:
            raise ArchiveError(f"Unknown failure point: {fail_at}")
        serialized = [_json(record.model_dump(mode="json")) for record in records]
        # Nested dictionaries remain mutable even on validated model instances.
        # Revalidate their serialized bytes before publishing anything.
        records = [Record.model_validate_json(data) for data in serialized]
        request_hash = _hash(_json({"records": [json.loads(b) for b in serialized], "expected": expected or {}}))
        with self._writer():
            manifests, existing = self._load()
            for manifest in manifests:
                if manifest["idempotency_key"] == idempotency_key:
                    if manifest["request_hash"] != request_hash:
                        raise IdempotencyError("Idempotency key reused with different content or preconditions")
                    self._sync_projection(manifests, existing)
                    return manifest["commit_id"]
            latest: dict[str, int] = {}
            for record in existing.values():
                latest[record.record_id] = max(latest.get(record.record_id, 0), record.revision)
            for record_id, revision in (expected or {}).items():
                _safe_id(record_id)
                if latest.get(record_id, 0) != revision:
                    raise ConflictError(f"CAS conflict for {record_id}: expected {revision}, found {latest.get(record_id, 0)}")
            refs = {r.ref for r in records}
            if len(refs) != len(records) or len({r.record_id for r in records}) != len(records):
                raise ConflictError("One revision per record is allowed in an atomic batch")
            for record in records:
                _safe_id(record.record_id)
                if record.agent_id != self.agent_id or record.user_id != self.user_id:
                    raise ArchiveError("Cross-user or cross-agent writes are forbidden")
                current = latest.get(record.record_id, 0)
                if record.revision != current + 1:
                    raise ConflictError(f"Revision must follow current version for {record.record_id}")
                if current:
                    old = existing[f"{record.record_id}@{current}"]
                    if old.record_kind in _IMMUTABLE_KINDS:
                        raise ConflictError("Original events/messages are immutable; append an interpretation instead")
                    if old.record_kind != record.record_kind or record.supersedes_revision != current:
                        raise ConflictError("New revision must retain kind and explicitly supersede current revision")
                elif record.supersedes_revision is not None:
                    raise ConflictError("Initial revision cannot supersede a missing record")
                for ref in record.source_refs:
                    if ref == record.ref or (ref not in existing and ref not in refs):
                        raise ArchiveError(f"Missing, self-referencing, or isolated source: {ref}")
            # Cycles within the batch cannot acquire external evidence later.
            visiting: set[str] = set()
            visited: set[str] = set()
            pending = {r.ref: r for r in records}
            def visit(ref: str) -> None:
                if ref in visiting:
                    raise ArchiveError("Cyclic source references")
                if ref in visited or ref not in pending:
                    return
                visiting.add(ref)
                for source in pending[ref].source_refs:
                    visit(source)
                visiting.remove(ref)
                visited.add(ref)
            for ref in pending:
                visit(ref)
            for record in records:
                if record.record_kind == "emotion_appraisal":
                    from myatri.contracts.emotion import validate_appraisal_sources
                    validate_appraisal_sources(record, {**existing, **{r.ref: r for r in records}})
                if record.record_kind == "emotion_retraction":
                    from myatri.contracts.emotion import validate_retraction_sources
                    validate_retraction_sources(record, {**existing, **{r.ref: r for r in records}})
                if record.record_kind == "event":
                    event = Event.model_validate(record.payload)
                    spans = event.source_spans
                    allowed_roles = {"assistant"} if event.reality_scope == "role_output" else {"user"}
                elif record.record_kind == "interpretation" and record.payload.get("source_spans"):
                    spans = [SourceSpan.model_validate(span) for span in record.payload["source_spans"]]
                    allowed_roles = {"user"}
                else:
                    continue
                for span in spans:
                    source = pending.get(span.message_ref) or existing.get(span.message_ref)
                    if source is None or source.record_kind != "raw_message" or span.message_ref not in record.source_refs:
                        raise ArchiveError("Source span must cite an available raw_message in source_refs")
                    if source.payload.get("role") not in allowed_roles:
                        raise ArchiveError("External event spans must cite user messages; role_output must cite assistant messages")
                    if span.end <= span.start or span.end > len(source.body_markdown) or source.body_markdown[span.start:span.end] != span.quote:
                        raise ArchiveError("Source span quote does not exactly match original text")
            commit_id = uuid.uuid4().hex
            entries = []
            for record, data in zip(records, serialized, strict=True):
                # Content addressing also keeps a pre-publication orphan from
                # blocking a later, differently authored retry of the revision.
                relative = Path("records") / _hash(record.record_id.encode()) / f"{record.revision}-{_hash(data)}.json"
                target = self.domain_path / relative
                if target.exists():
                    if target.read_bytes() != data:
                        raise IntegrityError("Existing immutable file was modified")
                else:
                    _atomic_write(target, data)
                entries.append({"ref": record.ref, "path": relative.as_posix(), "sha256": _hash(data)})
            if fail_at == "after_files":
                raise RuntimeError("Injected crash after_files")
            manifest = {"schema_version": "1.0", "sequence": len(manifests) + 1, "commit_id": commit_id,
                        "previous_commit": manifests[-1]["commit_id"] if manifests else None,
                        "agent_id": self.agent_id, "user_id": self.user_id, "idempotency_key": idempotency_key,
                        "request_hash": request_hash, "records": entries}
            manifest["manifest_sha256"] = _hash(_json(manifest))
            _atomic_write(self.manifest_path / f"{len(manifests) + 1:020d}-{commit_id}.json", _json(manifest))
            if fail_at == "after_manifest":
                raise RuntimeError("Injected crash after_manifest")
            manifests.append(manifest)
            existing.update(pending)
            self._projection_pending = getattr(self, "_projection_pending", 0) + 1
            if self._projection_pending >= getattr(self, "projection_every", 1) or fail_at == "after_projection":
                self._sync_projection(manifests, existing)
            if fail_at == "after_projection":
                raise RuntimeError("Injected crash after_projection")
            return commit_id

    def flush_projection(self):
        """Publish a rebuildable projection checkpoint; originals are already durable."""
        with self._writer():
            manifests, records = self._load()
            self._sync_projection(manifests, records)

    def _sync_projection(self, manifests, records):
        try:
            with closing(sqlite3.connect(self.db_path, timeout=30)) as connection, connection:
                watermark = connection.execute("SELECT value FROM metadata WHERE key='archive_watermark'").fetchone()
                current = watermark[0] if watermark else ""
                position = next((i for i, m in enumerate(manifests) if m["commit_id"] == current), None)
                if current and position is None:
                    raise sqlite3.DatabaseError("projection watermark outside archive")
                missing = manifests[position + 1:] if position is not None else manifests
                for manifest in missing:
                    for entry in manifest["records"]:
                        record = records[entry["ref"]]
                        connection.execute("INSERT OR IGNORE INTO records VALUES(?,?,?,?,?,?,?)", (
                            record.ref, record.record_id, record.revision, record.record_kind,
                            record.created_at, record.body_markdown, _json(record.model_dump(mode="json")).decode()))
                        connection.execute("INSERT INTO records_fts VALUES(?,?)", (record.ref, record.body_markdown))
                connection.execute("INSERT OR REPLACE INTO metadata VALUES('archive_watermark',?)", (manifests[-1]["commit_id"] if manifests else "",))
        except sqlite3.DatabaseError:
            try:
                self._rebuild(manifests, records)
            except sqlite3.DatabaseError:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(self.db_path) + suffix).unlink(missing_ok=True)
                self._rebuild(manifests, records)
        self._projection_pending = 0

    def _rebuild(self, manifests: list[dict], records: dict[str, Record]) -> None:
        # Transactional replacement: readers never observe half a multi-record
        # commit in this projection. The manifest archive remains authority.
        with closing(sqlite3.connect(self.db_path, timeout=30)) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS records(ref TEXT PRIMARY KEY,record_id TEXT,revision INTEGER,kind TEXT,created_at TEXT,body TEXT,json TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT)")
            connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5(ref UNINDEXED,body,tokenize='trigram')")
            connection.execute("DELETE FROM records")
            connection.execute("DELETE FROM records_fts")
            for record in records.values():
                connection.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?)", (record.ref,record.record_id,record.revision,record.record_kind,record.created_at,record.body_markdown,_json(record.model_dump(mode="json")).decode()))
                connection.execute("INSERT INTO records_fts VALUES(?,?)", (record.ref,record.body_markdown))
            connection.execute("INSERT OR REPLACE INTO metadata VALUES('archive_watermark',?)", (manifests[-1]["commit_id"] if manifests else "",))

    def rebuild_projection(self) -> None:
        with self._writer():
            manifests, records = self._load()
            try:
                self._rebuild(manifests, records)
            except sqlite3.DatabaseError:
                # A corrupt or deleted projection can always be reconstructed.
                for suffix in ("", "-wal", "-shm"):
                    Path(str(self.db_path) + suffix).unlink(missing_ok=True)
                self._rebuild(manifests, records)
            self._projection_pending = 0

    def recover(self) -> None:
        self.rebuild_projection()

    def verify_archive(self) -> list[str]:
        try:
            self._load(force=True)
        except ArchiveError as error:
            return [str(error)]
        return []

    def append_message(
        self,
        role: str,
        content: str,
        message_id: str | None = None,
        *,
        author_kind: str | None = None,
        **metadata: object,
    ) -> Record:
        """追加一条原始消息记录。

        ``author_kind`` 为 None 时保持原有行为：``user`` 记为 ``human``，
        其余角色记为 ``system``。显式传值时使用调用方给出的归属，但必须是
        契约层已定义的取值；其中 ``self_initiated`` 表示机器人自发起的合成
        轮次——它不是人类输入，不得被当成人类证据。归属只写入 Record 信封，
        不进入 payload，因此默认调用产生的记录内容完全不变。
        """
        resolved = ("human" if role == "user" else "system") if author_kind is None else author_kind
        if resolved not in AUTHOR_KINDS:
            raise ArchiveError(f"Unknown author_kind: {resolved!r}")
        if message_id:
            try:
                old = self.get_record(message_id)
            except KeyError:
                pass
            else:
                author_changed = author_kind is not None and old.author_kind != resolved
                if author_changed or old.record_kind != "raw_message" or old.body_markdown != content or old.payload != {"role": role, **metadata}:
                    raise IdempotencyError("Message ID reused with different content")
                return old
        record = Record(record_id=message_id or f"msg_{uuid.uuid4().hex}", revision=1, record_kind="raw_message",
                        agent_id=self.agent_id, user_id=self.user_id, author_kind=resolved,
                        body_markdown=content, payload={"role": role, **metadata})
        try:
            self.commit([record], f"message:{record.record_id}")
        except ConflictError:
            # A simultaneous append may publish the same message after our
            # initial read. Compare semantic content rather than generated time.
            old = self.get_record(record.record_id)
            author_changed = author_kind is not None and old.author_kind != resolved
            if author_changed or old.record_kind != "raw_message" or old.body_markdown != content or old.payload != record.payload:
                raise
            return old
        return record
