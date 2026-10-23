"""Snapshot-scoped mixed recall with RRF, typed paths, and original sources."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import re
import json
import sqlite3

from myatri.contracts import RecallBundle, Record
from myatri.memory import MemoryIndex
from myatri.memory.graph import MemoryGraph
from myatri.memory.snapshot import read_snapshot
from myatri.memory.text import record_search_text
from myatri.storage import ArchiveStore


def _grams(text: str, width: int = 2) -> set[str]:
    compact = re.sub(r"\s+", "", text.lower())
    return {compact[i:i + width] for i in range(max(0, len(compact) - width + 1))}


def _text(record: Record) -> str:
    return record_search_text(record)


def _day(record: Record) -> str:
    if record.record_kind == "event":
        return str(record.payload.get("occurred_at") or "")[:10]
    return str(record.payload.get("occurred_at") or record.created_at)[:10]


def _earliest_sources(query: str, candidates: dict[str, Record]) -> list[str]:
    """Locate retained conversation beginnings, not an inferred first meeting."""
    if not (re.search(r"最早|最开始|初次|第一次|刚认识", query)
            and re.search(r"认识|见面|聊天|对话|相遇|相识|记忆|说过|说了|聊了|聊的", query)):
        return []
    sources = []
    for ref, record in candidates.items():
        data = {"record_kind": record.record_kind, "payload": record.payload,
                "author_kind": record.author_kind, "created_at": record.created_at}
        # A second migration may wrap an already imported ATRI conversation.
        # Only traverse explicit human-import wrappers, never model claims.
        for _ in range(8):
            payload = data.get("payload", {})
            if data.get("author_kind") == "human_import" and payload.get("imported_private_memory"):
                if "群" in query or not payload.get("searchable_history"):
                    break
                data = payload.get("original", {})
                continue
            if data.get("record_kind") == "raw_message" and payload.get("role") in {"user", "assistant"}:
                observed = datetime.fromisoformat(data.get("created_at", record.created_at).replace("Z", "+00:00"))
                sources.append((observed.replace(tzinfo=observed.tzinfo or timezone.utc).timestamp(), ref))
            break
    return [ref for _, ref in sorted(sources)[:4]]


def _matches(record: Record, filters: dict) -> bool:
    default_kinds = {"event", "interpretation", "reflection", "theme", "temporary_state", "behavior_attempt",
                     "persona_change", "raw_message", "summary"}
    if not filters.get("kinds") and record.record_kind not in default_kinds:
        return False
    if filters.get("kinds") and record.record_kind not in filters["kinds"]:
        return False
    if any(filters.get(key) for key in ("date_from", "date_to", "person", "participants")) and record.record_kind != "event":
        return False
    if (filters.get("date_from") or filters.get("date_to")) and not _day(record):
        return False
    if filters.get("date_from") and _day(record) < filters["date_from"][:10]:
        return False
    if filters.get("date_to") and _day(record) > filters["date_to"][:10]:
        return False
    people = filters.get("participants") or ([filters["person"]] if filters.get("person") else [])
    if isinstance(people, str):
        people = [people]
    if people and not all(person in record.payload.get("participants", []) for person in people):
        return False
    if record.record_kind == "temporary_state" and not filters.get("include_historical", False):
        if record.payload.get("status", "open") not in {"open", "paused"}:
            return False
    return True


class RecallService:
    def __init__(self, store: ArchiveStore, index: MemoryIndex | None = None):
        self.store, self.index = store, index

    def recall(self, query: str, filters: dict | None = None, snapshot: str | None = None, budget: int = 4000) -> RecallBundle:
        if budget < 1:
            raise ValueError("Recall budget must be positive")
        archive = read_snapshot(self.store, snapshot)
        selected_snapshot = archive.watermark
        records = list(archive.latest)
        selected_filters = dict(filters or {})
        latest_revisions = {r.record_id: r.revision for r in records}
        if selected_filters.get("include_historical"):
            for record in list(records):
                if record.record_kind == "temporary_state":
                    records.extend(archive.records[f"{record.record_id}@{revision}"] for revision in range(1, record.revision))
        available = {r.ref: r for r in records}
        query_limitations = []
        date_match = re.search(r"(\d{4})[-年](\d{1,2})[-月](\d{1,2})日?", query)
        if date_match and "date_from" not in selected_filters and "date_to" not in selected_filters:
            year, month, day = map(int, date_match.groups())
            try:
                date = datetime(year, month, day).date().isoformat()
            except ValueError:
                # User prose may contain typos or discuss an impossible date.
                # It remains searchable text rather than crashing the turn.
                query_limitations.append("查询含无效日历日期，保留原文进行词面检索，未应用自动日期过滤。")
            else:
                selected_filters.update(date_from=date, date_to=date)
        # Only names already stored in this isolated snapshot are recognized.
        known_people = {person for r in records if r.record_kind == "event" for person in r.payload.get("participants", []) if isinstance(person, str) and person}
        mentioned = sorted(person for person in known_people if person in query)
        if mentioned and "participants" not in selected_filters and "person" not in selected_filters:
            selected_filters["participants"] = mentioned
        allowed_refs = selected_filters.pop("candidate_refs", None)
        allowed_refs = set(allowed_refs) if allowed_refs is not None else None
        candidates = {ref: r for ref, r in available.items()
                      if (allowed_refs is None or ref in allowed_refs) and _matches(r, selected_filters)}
        earliest = _earliest_sources(query, candidates)
        reasons: dict[str, list[str]] = defaultdict(list)
        ranked_channels: list[tuple[str, list[str]]] = []
        installed = self.index.capture_state() if self.index else None
        watermark = installed["watermark"] if installed else None
        compatible_index = bool(installed and installed["generation"] and watermark == selected_snapshot
                                and self.index.store.domain_path == self.store.domain_path)
        usable_base = compatible_index
        unchanged = set(available)
        if (installed and installed["generation"] and watermark and not compatible_index
                and self.index.store.domain_path == self.store.domain_path):
            base = read_snapshot(self.store, watermark)
            if base.sequence <= archive.sequence and all(ref in archive.records for ref in base.records):
                unchanged = {r.ref for r in base.latest if r.ref in available
                             and available[r.ref].content_hash == r.content_hash}
                usable_base = True
        limitations = [*query_limitations, *(installed["limitations"] if compatible_index else [])]
        if not installed or not installed["generation"]:
            limitations.append("向量索引未建立，使用档案全文/时间/人物回退。")
        elif usable_base and not compatible_index:
            limitations.append("索引水位说明：新增记录尚未并入基础索引；本轮仍可读取原文，并使用已验证基础向量与当前增量检索。这不表示旧记忆丢失或不可读。")
        elif not compatible_index:
            limitations.append("索引水位与请求快照不一致；跳过该世代的向量、主题和图，使用请求快照的档案词面与语义关系回退。")

        if compatible_index:
            graph = installed["graph"]
        else:
            # Both endpoints and the relation record itself must belong to the
            # requested snapshot. Historical source revisions remain valid.
            graph = MemoryGraph([row.ref for row in records if row.record_kind == "event"])
            edges = [row for row in archive.latest if row.record_kind == "edge"
                     and row.payload.get("from_ref") in available and row.payload.get("to_ref") in available]
            graph.apply_semantic_edges(edges, set(archive.records))

        # SQLite trigram narrows candidates without depending on English token
        # segmentation. Archive lexical scoring supplies short-query fallback.
        fts_refs: list[str] = []
        grams3 = sorted(_grams(query, 3))[:32]
        if grams3:
            expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in grams3)
            try:
                with closing(sqlite3.connect(self.store.db_path)) as connection:
                    # A read transaction pins the watermark and FTS corpus
                    # together; future documents must not change past ranking.
                    connection.execute("BEGIN")
                    projection = connection.execute("SELECT value FROM metadata WHERE key='archive_watermark'").fetchone()
                    if projection and projection[0] == (selected_snapshot or ""):
                        fts_refs = [row[0] for row in connection.execute("SELECT ref FROM records_fts WHERE records_fts MATCH ? ORDER BY rank LIMIT 100", (expression,)) if row[0] in candidates]
                    else:
                        limitations.append("FTS 水位与请求快照不一致，使用该快照的档案词面排序。")
            except sqlite3.DatabaseError:
                limitations.append("FTS 投影不可用，使用档案词面扫描回退；可运行 memory rebuild-index。")
        if fts_refs:
            ranked_channels.append(("Chinese trigram FTS", fts_refs))
        grams = _grams(query)
        lexical = []
        for ref, record in candidates.items():
            text = _text(record)
            score = len(grams & _grams(text)) / max(1, len(grams))
            if query and query.lower() in text.lower():
                score += 1
            if score > 0:
                lexical.append((score, ref))
        ranked_channels.append(("archive lexical", [ref for _, ref in sorted(lexical, key=lambda item: (-item[0], item[1]))]))
        if any(word in query for word in ("为什么", "为何", "变化", "改变", "更愿意", "人格", "以前", "后来")):
            relevant_changes = [(score, ref) for score, ref in lexical if candidates[ref].record_kind == "persona_change"]
            if relevant_changes:
                ranked_channels.append(("persona change history", [ref for _, ref in sorted(relevant_changes, key=lambda item: (-item[0], item[1]))]))
        if any(selected_filters.get(key) for key in ("date_from", "date_to", "person", "participants")):
            ranked_channels.append(("explicit time/person filter", sorted(candidates, key=lambda ref: (_day(candidates[ref]), ref), reverse=True)))
        if usable_base:
            for view in ("event", "feeling", "theme", "temporary_state"):
                try:
                    hits = self.index.query(query, view=view,
                        limit=30, _snapshot=installed, _allowed_refs=set(candidates) & unchanged)
                except RuntimeError:
                    limitations.append(f"{view} 外部 Embedding 查询需要异步 provider；同步回忆已采用词面回退。")
                    hits = []
                refs = [hit["ref"] for hit in hits if hit["ref"] in candidates and hit["ref"] in unchanged][:30]
                if refs:
                    ranked_channels.append((f"vector:{view}", refs))
                if not compatible_index:
                    provider = installed["providers"].get(view)
                    vector_index = installed["indexes"].get(view)
                    embed = getattr(provider, "transform", None) or getattr(provider, "embed_sync", None)
                    if vector_index is not None and callable(embed):
                        delta = [(ref, self.index._texts(record).get(view, "")) for ref, record in candidates.items()
                                 if ref not in unchanged and self.index._texts(record).get(view)]
                        if delta:
                            batch = embed([query, *[text for _, text in delta]], vector_index.space_id)
                            meta = vector_index.rows[0]
                            if (batch.space_id, batch.model, batch.model_revision, batch.dimension, batch.normalization) != (
                                vector_index.space_id, meta["model"], meta["model_revision"], meta["dimension"], meta["normalization"]):
                                raise ValueError("delta embedding provider changed revision or space")
                            q = batch.vectors[0]
                            ranked_delta = sorted(((sum(a*b for a, b in zip(q, vector, strict=True)), ref)
                                for (ref, _), vector in zip(delta, batch.vectors[1:], strict=True)), reverse=True)
                            ranked_channels.append((f"vector-delta:{view}", [ref for score, ref in ranked_delta if score > 0][:30]))
        if compatible_index:
            query_grams = _grams(query)
            theme_scores = []
            for theme_id, theme in installed["themes"].items():
                material_text = theme.get("name", "") + " " + theme.get("summary", "")
                material_text += " ".join(_text(candidates[ref]) for ref in theme["members"] if ref in candidates)
                overlap = len(query_grams & _grams(material_text))
                if overlap:
                    theme_scores.append((overlap, theme_id))
            theme_refs = []
            for _, theme_id in sorted(theme_scores, reverse=True):
                theme_refs.extend(ref for ref, strength in sorted(installed["themes"][theme_id]["memberships"].items(), key=lambda pair: (-pair[1], pair[0])) if ref in candidates and ref not in theme_refs)
            if theme_refs:
                ranked_channels.append(("theme membership", theme_refs))
        scores: dict[str, float] = defaultdict(float)
        for channel, refs in ranked_channels:
            for rank, ref in enumerate(dict.fromkeys(refs), 1):
                scores[ref] += 1 / (60 + rank)
                reasons[ref].append(channel)
        ranked = sorted(scores, key=lambda ref: (-scores[ref], ref))
        if earliest:
            ranked = [*earliest, *[ref for ref in ranked if ref not in earliest]]
            for ref in earliest:
                reasons[ref].append("earliest retained conversation source; not proof of first real meeting")
            limitations.append("早期原文定位：" + ", ".join(earliest)
                + "。这是当前保留范围的开头，不证明现实中首次认识；正文未注入时可read_record分页读取，不能据此声称没有记录。")
        paths: list[list[str]] = []
        counter_refs: list[str] = []
        # BehaviorAttempt uses a controlled decision label as its body, so its
        # semantic content is in the linked intent/action/feedback. Retrieve
        # those explicit relationships instead of relying on label keywords.
        seed_state_refs = {candidates[ref].record_id: ref for ref in ranked if candidates[ref].record_kind == "temporary_state"}
        seed_refs = set(ranked)
        for ref, record in candidates.items():
            if record.record_kind != "behavior_attempt" or ref in ranked:
                continue
            intent_ref = record.payload.get("intent_ref", "")
            state_id = intent_ref.rsplit("@", 1)[0]
            related = [source for source in [*record.payload.get("action_refs", []), *record.payload.get("observed_feedback_refs", [])] if source in seed_refs]
            origin = seed_state_refs.get(state_id) or (related[0] if related else None)
            if origin:
                ranked.append(ref)
                reasons[ref].append("explicit intent/action/feedback link")
                paths.append([origin, ref])
        if ranked:
            subgraph = graph.get_subgraph(ranked[:4], depth=1, limit=20)
            for path in subgraph["paths"]:
                if len(path) > 1 and all(ref in available for ref in path):
                    paths.append(path)
                    target = path[-1]
                    if target in candidates and target not in ranked:
                        ranked.append(target)
                        reasons[target].append("typed graph expansion")
            for edge in subgraph["edges"]:
                if edge["type"] == "contradicts":
                    for ref in (edge["from_ref"], edge["to_ref"]):
                        if ref in candidates and ref not in counter_refs:
                            counter_refs.append(ref)
                            reasons[ref].append("counterevidence:contradicts")
        # Preserve counterexamples before a budgeted selection. Evidence roots
        # are read by revision, so quoted text never comes from generated prose.
        ranked = list(dict.fromkeys(ranked[:4] + counter_refs + ranked[4:]))
        output: list[Record] = []
        emitted: set[str] = set()
        used = 0
        source_cache = dict(archive.records)
        native_sources = {}
        native_conflicts = set()
        for record in archive.latest:
            channel = record.payload.get("channel_source", {})
            if record.record_kind == "raw_message" and channel.get("message_id"):
                native_id = channel["message_id"]
                text = channel.get("text", record.body_markdown)
                if native_id in native_sources and native_sources[native_id][1] != text:
                    native_conflicts.add(native_id)
                native_sources[native_id] = (record, text)
        def load_source(ref: str) -> Record | None:
            if ref not in source_cache:
                limitations.append(f"来源 {ref} 在请求快照不可见。")
                return None
            return source_cache[ref]

        def quoted_group(record: Record) -> list[Record]:
            # Exact quoted spans always travel with their full original raw
            # messages. Runtime logs and transitive derivations are pointers.
            group = [record]
            for ref in selected_filters.get("evidence_companions", {}).get(record.ref, []):
                companion = load_source(ref)
                if companion is not None and companion.ref not in {r.ref for r in group}:
                    group.append(companion)
            for span in [span for member in list(group) for span in member.payload.get("source_spans", [])]:
                source = load_source(span["message_ref"])
                if source is not None and source.ref not in {r.ref for r in group}:
                    group.append(source)
            for member in list(group):
                if member.record_kind != "raw_message":
                    continue
                channel = member.payload.get("channel_source", {})
                quoted_id = channel.get("reply_to_id") or channel.get("reply_to_message_id")
                if not quoted_id and member.record_id.startswith("wx_"):
                    try:
                        original = json.loads(member.body_markdown)
                    except ValueError:
                        original = None
                    if isinstance(original, dict):
                        quoted_id = original.get("reply_to_id")
                if quoted_id in native_sources and quoted_id not in native_conflicts:
                    quoted = native_sources[quoted_id][0]
                    if quoted.ref not in {r.ref for r in group}:
                        group.append(quoted)
            return group

        def cost(group: list[Record]) -> int:
            return sum(len(r.body_markdown) + len(str(r.payload)) for r in group if r.ref not in emitted)

        def include(group: list[Record], origin: str) -> None:
            nonlocal used
            used += cost(group)
            for selected in group:
                if selected.ref in emitted:
                    continue
                output.append(selected)
                emitted.add(selected.ref)
                if selected.ref != origin:
                    reasons[selected.ref].append(f"original source for {origin}")
                    paths.append([origin, selected.ref])
                if selected.record_kind == "temporary_state":
                    status = selected.payload.get("status", "open")
                    reasons[selected.ref].append(f"temporary state status={status}; recall is not activation or practice")
                    if selected.revision != latest_revisions.get(selected.record_id):
                        reasons[selected.ref].append("historical revision; not current state")

        selected_candidates = []
        for ref in ranked:
            record = candidates[ref]
            group = quoted_group(record)
            if used + cost(group) > budget:
                limitations.append(f"回忆预算不足，候选 {ref} 及其逐字原文组未注入；可 expand/read_record。")
                continue
            # A personality change preferentially carries one independent
            # external event and its raw source, without traversing every log.
            roots = record.payload.get("event_roots", []) if record.record_kind == "persona_change" else []
            if roots:
                root = load_source(roots[0])
                if root is not None:
                    root_group = quoted_group(root)
                    combined = list({r.ref: r for r in [*group, *root_group]}.values())
                    if used + cost(combined) <= budget:
                        group = combined
                    else:
                        limitations.append(f"{ref} 的独立事件原文暂未展开，保留 event_roots 版本指针。")
            include(group, ref)
            selected_candidates.append(record)
        # Expand lower-priority derivations only after preserving candidates.
        # The authority retains every source_refs link; callers can explicitly
        # read/expand omitted logs rather than losing a relevant change itself.
        for record in selected_candidates:
            priority = [*record.payload.get("event_roots", []), *record.payload.get("patch", {}).get("counter_refs", []), *record.source_refs]
            pending = [(record.ref, ref) for ref in dict.fromkeys(priority)]
            visited = {record.ref}
            omitted = []
            while pending:
                origin, ref = pending.pop(0)
                if ref in visited:
                    continue
                visited.add(ref)
                source = load_source(ref)
                if source is None:
                    omitted.append(ref)
                    continue
                if source.record_kind in {"cognitive_run", "activation", "usage"} and source.ref not in emitted:
                    omitted.append(ref)
                    continue
                group = quoted_group(source)
                if used + cost(group) > budget:
                    omitted.append(ref)
                    continue
                include(group, origin)
                pending.extend((source.ref, child) for child in source.source_refs if child not in visited)
            if omitted:
                limitations.append(f"来源分层预算：{record.ref} 已保留，未展开版本引用 {', '.join(omitted[:8])}"
                                   + (f" 等 {len(omitted)} 项" if len(omitted) > 8 else "") + "；可 read_record/expand 获取完整材料。")
        # Paths and counter references expose only actually returned materials.
        paths = [path for path in paths if all(ref in emitted for ref in path)]
        return RecallBundle(snapshot=selected_snapshot, records=output,
                            reasons={ref: list(dict.fromkeys(reasons[ref])) for ref in emitted}, paths=paths,
                            counter_refs=[ref for ref in counter_refs if ref in emitted], index_watermark=watermark,
                            limitations=list(dict.fromkeys(limitations)))

    def read_record(self, ref: str, snapshot: str | None = None) -> Record:
        record_id, revision = ref.rsplit("@", 1)
        return self.store.get_record(record_id, int(revision), snapshot)

    def expand(self, refs: list[str], snapshot: str | None = None) -> list[Record]:
        return [self.read_record(ref, snapshot) for ref in refs]
