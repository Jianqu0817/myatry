"""Small adapted A-MEM linked-memory expansion kernel (MIT).

Source: https://github.com/agiresearch/A-mem
Commit: ceffb860f0712bbae97b184d440df62bc910ca8d
File: agentic_memory/memory_system.py
Function: AgenticMemorySystem.search_agentic, lines 554-583.
Copyright (c) 2025 AGI Research. See LICENSES/upstream/A-MEM-LICENSE.

Extracted the bounded, seen-ID deduplication loop. MYATRI supplies ordered
(origin, linked ID) pairs instead of mutable MemoryNote objects and owns edge
type filtering, snapshot membership, paths and depth. The iterator consumes
all candidates even after its allowance is filled, so the caller can retain
incident edges. The upstream final memories[:k] truncation is not copied.
No Chroma client, model, mutable memory update or network operation is used.
"""


def iter_unique_neighbors(candidates, seen_ids, k):
    """Yield at most k unseen links, adding IDs to the caller's visited set."""
    neighbor_count = 0
    for origin, link_id in candidates:
        if link_id not in seen_ids and neighbor_count < k:
            seen_ids.add(link_id)
            neighbor_count += 1
            yield origin, link_id
