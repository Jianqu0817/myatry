"""One coherent archive read, with current records and pinned historical sources."""
from __future__ import annotations

from dataclasses import dataclass

from myatri.contracts import Record
from myatri.storage import ArchiveStore


@dataclass(frozen=True)
class ArchiveSnapshot:
    watermark: str | None
    sequence: int
    records: dict[str, Record]
    latest: tuple[Record, ...]


def read_snapshot(store: ArchiveStore, snapshot: str | None = None) -> ArchiveSnapshot:
    # Loading once also pins an empty archive. snapshot() followed by a second
    # read cannot pin emptiness because the public None means "latest".
    manifests, records = store._load(snapshot)
    latest: dict[str, Record] = {}
    for record in records.values():
        if record.record_id not in latest or record.revision > latest[record.record_id].revision:
            latest[record.record_id] = record
    return ArchiveSnapshot(
        manifests[-1]["commit_id"] if manifests else None,
        len(manifests), records,
        tuple(sorted(latest.values(), key=lambda row: (row.created_at, row.record_id))),
    )
