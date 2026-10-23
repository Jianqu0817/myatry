"""One verified, read-only archive view for a synchronous assembly pass.

Never retained across an await or a turn. The authoritative store validates
files on entry; consumers receive independent records, including old revisions.
"""
from copy import deepcopy

from myatri.storage.archive import ArchiveError, _safe_id


class ContextReadView:
    def __init__(self, store):
        self._manifests, self._records = store._load()
        self._snapshots = {}
        for name in ("root", "domain_path", "db_path", "agent_id", "user_id"):
            setattr(self, name, getattr(store, name))

    def snapshot(self):
        return self._manifests[-1]["commit_id"] if self._manifests else None

    def _at(self, snapshot):
        if snapshot is None:
            return self._manifests, self._records
        if snapshot not in self._snapshots:
            manifests, refs = [], set()
            for manifest in self._manifests:
                manifests.append(manifest)
                refs.update(entry["ref"] for entry in manifest["records"])
                if manifest["commit_id"] == snapshot:
                    break
            else:
                raise ArchiveError(f"Unknown snapshot in current isolation domain: {snapshot}")
            self._snapshots[snapshot] = manifests, {ref: self._records[ref] for ref in refs}
        return self._snapshots[snapshot]

    def _load(self, snapshot=None, *, force=False):
        if force:
            raise RuntimeError("Forced integrity checks require the authoritative archive")
        return deepcopy(self._at(snapshot))

    def list_records(self, kind=None, snapshot=None):
        latest = {}
        for record in self._at(snapshot)[1].values():
            if record.record_id not in latest or record.revision > latest[record.record_id].revision:
                latest[record.record_id] = record
        return deepcopy(sorted((r for r in latest.values() if kind is None or r.record_kind == kind),
                               key=lambda r: (r.created_at, r.record_id)))

    def get_record(self, record_id, revision=None, snapshot=None):
        _safe_id(record_id)
        rows = [r for r in self._at(snapshot)[1].values() if r.record_id == record_id
                and (revision is None or r.revision == revision)]
        if not rows:
            raise KeyError(f"Record unavailable in current domain/snapshot: {record_id}@{revision}")
        return deepcopy(max(rows, key=lambda r: r.revision))
