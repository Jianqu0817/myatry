"""Persistent, atomically published memory-index generations."""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
import uuid

from myatri.contracts import EmbeddingBatch, Record, digest
from myatri.storage import ArchiveStore
from myatri.storage.archive import _atomic_write, _json
from .clustering import cluster
from .embeddings import LocalTfidfEmbeddingProvider, cache_key, text_hash
from .graph import MemoryGraph
from .snapshot import read_snapshot
from .themes import build_themes, theme_materials
from .text import record_search_text
from .vector_index import ExactVectorIndex


class MemoryIndex:
    def __init__(self, store: ArchiveStore, provider: Any = None, space_id: str | None = None,
                 *, k: int = 5, threshold: float = 0.08, seed: int = 17):
        self.store, self.provider, self.requested_space_id = store, provider, space_id
        self.k, self.threshold, self.seed = k, threshold, seed
        self._state_lock = threading.RLock()
        self._sequence = 0
        self.path = store.domain_path / "indexes"
        self.path.mkdir(parents=True, exist_ok=True)
        self.generation: str | None = None
        self.watermark: str | None = None
        self.indexes: dict[str, ExactVectorIndex] = {}
        self.providers: dict[str, Any] = {}
        self.graph = MemoryGraph()
        self.clusters: dict = {}
        self.themes: dict[str, dict] = {}
        self.embedding_cache: dict[str, dict] = {}
        self.limitations: list[str] = []
        self.cache_hits = 0
        if (self.path / "current.json").exists():
            self.load()

    @staticmethod
    def _texts(record: Record) -> dict[str, str]:
        payload = record.payload
        if record.record_kind == "temporary_state":
            return {"temporary_state": str(payload.get("text") or record.body_markdown)}
        if record.record_kind == "theme":
            return {"theme": record.body_markdown or str(payload.get("description") or payload.get("name") or "")}
        if record.record_kind not in {"raw_message", "event", "interpretation", "reflection", "persona_change", "summary"}:
            return {}
        event_text = record_search_text(record) if record.record_kind == "persona_change" else str(payload.get("fact_summary") or record.body_markdown)
        texts = {"event": event_text}
        feelings = [payload[key] for key in ("feeling", "feelings", "affect_summary", "expectation", "expectations") if payload.get(key)]
        if feelings:
            texts["feeling"] = "主观感受/期待：" + "；".join(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) for value in feelings)
        return texts

    def capture_state(self) -> dict:
        """Pin one installed generation while another thread builds its successor."""
        with self._state_lock:
            return {"generation": self.generation, "watermark": self.watermark,
                    "indexes": self.indexes, "providers": self.providers,
                    "graph": self.graph, "clusters": self.clusters, "themes": self.themes,
                    "embedding_cache": self.embedding_cache, "cache_hits": self.cache_hits,
                    "limitations": self.limitations}

    def _prepare_rebuild(self) -> dict:
        # Capture lineage/cache before reading the archive. Another build can
        # publish while that read is in progress; consulting its newer themes
        # afterwards would copy future summaries into this older generation,
        # even if monotone publication later rejects its current pointer.
        installed = self.capture_state()
        snapshot = read_snapshot(self.store)
        records = snapshot.latest
        record_map = {r.ref: r for r in records}
        material: dict[str, list[tuple[Record, str]]] = {}
        for record in records:
            for view, text in self._texts(record).items():
                if text.strip():
                    material.setdefault(view, []).append((record, text))
        cache = dict(installed["embedding_cache"])
        plans = []
        for view, items in sorted(material.items()):
            texts = [text for _, text in items]
            if self.provider is None or isinstance(self.provider, LocalTfidfEmbeddingProvider):
                provider = LocalTfidfEmbeddingProvider(view).fit(texts)
                space_id = provider.space_id
            else:
                provider = self.provider
                space_id = self.requested_space_id or getattr(provider, "space_id", None)
                if not space_id:
                    raise ValueError("External embedding providers require an explicit space_id")
            # Cache reuse is permitted only when provider metadata pins a model
            # revision. A provider without it must return actual fresh metadata.
            identity = {key: getattr(provider, key, None) for key in ("model", "model_revision", "dimension", "normalization")}
            stable_identity = all(identity.values())
            lookup_keys = [digest({"space": space_id, "view": view, "text": text_hash(text), "identity": identity}) for text in texts]
            cached = [cache.get(key) if stable_identity else None for key in lookup_keys]
            missing_positions = [i for i, row in enumerate(cached) if row is None]
            plans.append({"view": view, "items": items, "texts": texts,
                          "provider": provider, "space_id": space_id,
                          "stable_identity": stable_identity, "lookup_keys": lookup_keys,
                          "cached": cached, "missing_positions": missing_positions})
        return {"snapshot": snapshot, "records": records, "record_map": record_map,
                "cache": cache, "previous_themes": installed["themes"], "plans": plans}

    async def rebuild(self) -> str:
        # Disk scans, fitting, matrices, clustering, JSON and publication are
        # synchronous work. External async providers stay on their caller's
        # loop so loop-bound clients/locks are never moved to another loop.
        prepared = await asyncio.to_thread(self._prepare_rebuild)
        batches = {}
        for plan in prepared["plans"]:
            positions = plan["missing_positions"]
            if positions:
                batches[plan["view"]] = await plan["provider"].embed(
                    [plan["texts"][i] for i in positions], plan["space_id"])
        return await asyncio.to_thread(self._finish_rebuild, prepared, batches)

    def _finish_rebuild(self, prepared: dict, batches: dict[str, EmbeddingBatch]) -> str:
        snapshot = prepared["snapshot"]
        records, record_map, cache = prepared["records"], prepared["record_map"], prepared["cache"]
        indexes: dict[str, ExactVectorIndex] = {}
        providers: dict[str, Any] = {}
        provider_states: dict[str, dict] = {}
        hits = 0
        for plan in prepared["plans"]:
            view, items, texts = plan["view"], plan["items"], plan["texts"]
            provider, space_id = plan["provider"], plan["space_id"]
            cached, missing_positions = plan["cached"], plan["missing_positions"]
            if missing_positions:
                batch = batches[view]
                if batch.space_id != space_id or len(batch.vectors) != len(missing_positions):
                    raise ValueError("Embedding provider returned another space or incomplete batch")
                if batch.normalization != "l2":
                    raise ValueError("Exact cosine baseline requires declared l2 normalization")
                for position, vector in zip(missing_positions, batch.vectors, strict=True):
                    cached[position] = {"vector": vector, "space_id": batch.space_id, "view": view,
                                        "model": batch.model, "model_revision": batch.model_revision,
                                        "dimension": batch.dimension, "normalization": batch.normalization,
                                        "text_hash": text_hash(texts[position]), "cache_key": cache_key(texts[position], view, batch)}
                    if plan["stable_identity"]:
                        cache[plan["lookup_keys"][position]] = cached[position]
            hits += len(items) - len(missing_positions)
            rows = []
            metadata_identity = set()
            for (record, _), entry in zip(items, cached, strict=True):
                assert entry is not None
                event_refs = [ref for ref in record.source_refs if ref in record_map and record_map[ref].record_kind == "event"]
                anchor = event_refs[0] if view == "feeling" and len(event_refs) == 1 else record.ref
                rows.append({**entry, "ref": record.ref, "event_ref": anchor, "record_id": record.record_id, "revision": record.revision})
                metadata_identity.add((entry["space_id"], entry["model"], entry["model_revision"], entry["dimension"], entry["normalization"]))
            if len(metadata_identity) != 1:
                raise ValueError("Provider/model revision changed within one vector space")
            indexes[view] = ExactVectorIndex(space_id, rows[0]["dimension"], rows)
            providers[view] = provider
            provider_states[view] = provider.state() if isinstance(provider, LocalTfidfEmbeddingProvider) else {"type": "external", "space_id": space_id, "model": rows[0]["model"], "model_revision": rows[0]["model_revision"]}
        generation = f"index_{uuid.uuid4().hex}"
        graph = MemoryGraph([r.ref for r in records if r.record_kind == "event"], generation=generation)
        graph.upsert_similarity_edges(indexes, k=self.k, threshold=self.threshold)
        # Old-version edges are retained in the archive; current graph only
        # includes endpoints visible in this generation's current revision set.
        edges = [r for r in records if r.record_kind == "edge" and r.payload.get("from_ref") in record_map and r.payload.get("to_ref") in record_map]
        # Index membership is latest-only; evidence is an immutable pinned
        # revision and remains valid after its record gains a newer revision.
        graph.apply_semantic_edges(edges, set(snapshot.records))
        clustered = cluster(graph, seed=self.seed)
        themes, lineage = build_themes(clustered, graph, record_map, prepared["previous_themes"])
        for theme_id, theme in themes.items():
            model_record = next((record for record in records if record.record_kind == "theme" and record.record_id == theme_id), None)
            if model_record is not None and model_record.payload.get("material_hash") == theme["material_hash"]:
                theme.update(name=model_record.payload.get("name") or model_record.payload.get("label") or theme["name"],
                             summary=model_record.body_markdown, naming_source="model" if model_record.author_kind == "model" else "imported",
                             needs_summary=False, summary_ref=model_record.ref)
            elif model_record is not None:
                theme.update(name=f"暂定主题 {list(themes).index(theme_id) + 1}", summary="",
                             naming_source="algorithmic_placeholder", needs_summary=True)
        clustered.lineage = lineage
        limitations = ["本地 char-ngram TF-IDF 是词面向量基线，不是神经语义 Embedding；否定与跨话题语义能力有限。"] if self.provider is None or isinstance(self.provider, LocalTfidfEmbeddingProvider) else []
        limitations += ["群落主题仍为 provisional；算法占位标签不代表 A3 模型语义摘要已验证。", "感受视角缺失时跳过并重新归一化可用视角权重；不会伪造感受。"]
        state = {"schema_version": "1.0", "agent_id": self.store.agent_id, "user_id": self.store.user_id,
                 "generation": generation, "watermark": snapshot.watermark,
                 "archive_sequence": snapshot.sequence, "providers": provider_states,
                 "indexes": {view: {"space_id": index.space_id, "dimension": index.dimension, "rows": index.rows} for view, index in indexes.items()},
                 "graph": graph.state(), "clusters": clustered.model_dump(mode="json"), "themes": themes,
                 "embedding_cache": cache, "cache_hits": hits, "limitations": limitations,
                 "params": {"k": self.k, "threshold": self.threshold, "seed": self.seed}}
        checksum = digest(state)
        # Large immutable generation writes do not hold the archive writer.
        _atomic_write(self.path / "generations" / f"{generation}.json", _json({"data": state, "sha256": checksum}))
        published = False
        with self.store._writer():
            pointer_path = self.path / "current.json"
            pointer = json.loads(pointer_path.read_text(encoding="utf-8")) if pointer_path.exists() else None
            # Locks make each publication atomic; comparing archive sequence
            # additionally prevents a slow older build from undoing a newer one.
            current_sequence = self._pointer_sequence(pointer) if pointer else -1
            if snapshot.sequence >= current_sequence:
                _atomic_write(pointer_path, _json(self._pointer(state, checksum)))
                self._install(state, indexes=indexes, providers=providers)
                published = True
        if not published:
            self._load_current_monotone()
        return self.capture_state()["generation"]

    async def update(self) -> str:
        watermark = await asyncio.to_thread(self.store.snapshot)
        installed = self.capture_state()
        if installed["generation"] and installed["watermark"] == watermark:
            return installed["generation"]
        # V1 baseline intentionally uses full rebuild as the correctness oracle.
        # It does not claim exact global Leiden results from local updates.
        return await self.rebuild()

    def _archive_sequence(self, watermark: str | None) -> int:
        if watermark is None:
            return 0
        # The manifest filename is immutable and contains its sequence and ID;
        # publication need not parse every record while holding the writer lock.
        for path in self.store.manifest_path.glob("*.json"):
            sequence, _, commit_id = path.stem.partition("-")
            if commit_id == watermark:
                return int(sequence)
        raise ValueError("Index watermark is unavailable in this archive")

    @staticmethod
    def _pointer(state: dict, checksum: str) -> dict:
        return {"generation": state["generation"], "sha256": checksum,
                "watermark": state["watermark"], "archive_sequence": state["archive_sequence"]}

    def _pointer_sequence(self, pointer: dict) -> int:
        if "archive_sequence" not in pointer or "watermark" not in pointer:
            state = self._read_generation(pointer["generation"], pointer)
            return state["archive_sequence"]
        sequence = self._archive_sequence(pointer["watermark"])
        if pointer["archive_sequence"] != sequence:
            raise ValueError("Index pointer archive sequence mismatch")
        return sequence

    def _read_generation(self, generation: str, pointer: dict | None = None) -> dict:
        if not generation.startswith("index_") or not generation[6:].isalnum():
            raise ValueError("Unsafe index generation")
        envelope = json.loads((self.path / "generations" / f"{generation}.json").read_text(encoding="utf-8"))
        state = envelope["data"]
        if digest(state) != envelope["sha256"] or (pointer and pointer["sha256"] != envelope["sha256"]):
            raise ValueError("Index generation checksum mismatch")
        if state["agent_id"] != self.store.agent_id or state["user_id"] != self.store.user_id or state["generation"] != generation:
            raise ValueError("Index generation isolation/identity mismatch")
        sequence = self._archive_sequence(state["watermark"])
        if state.get("archive_sequence", sequence) != sequence:
            raise ValueError("Index generation archive sequence mismatch")
        if pointer and (("watermark" in pointer and pointer["watermark"] != state["watermark"]) or
                        ("archive_sequence" in pointer and pointer["archive_sequence"] != sequence)):
            raise ValueError("Index pointer watermark mismatch")
        # Older saved generations predate this metadata and remain loadable.
        state["archive_sequence"] = sequence
        return state

    def _install(self, state: dict, *, indexes=None, providers=None, monotone=False) -> None:
        indexes = indexes if indexes is not None else {view: ExactVectorIndex(data["space_id"], data["dimension"], data["rows"]) for view, data in state["indexes"].items()}
        providers = providers if providers is not None else {view: LocalTfidfEmbeddingProvider.from_state(data) if data["type"] == "local_tfidf" else self.provider for view, data in state["providers"].items()}
        graph = MemoryGraph(**state["graph"])
        with self._state_lock:
            if monotone and self._sequence > state["archive_sequence"]:
                return
            self.generation, self.watermark, self._sequence = state["generation"], state["watermark"], state["archive_sequence"]
            self.indexes, self.providers, self.graph = indexes, providers, graph
            self.clusters, self.themes = state["clusters"], state["themes"]
            self.embedding_cache, self.limitations, self.cache_hits = state["embedding_cache"], state["limitations"], state["cache_hits"]

    def _load_current_monotone(self) -> None:
        pointer = json.loads((self.path / "current.json").read_text(encoding="utf-8"))
        self._install(self._read_generation(pointer["generation"], pointer), monotone=True)

    def load(self, generation: str | None = None) -> None:
        pointer = json.loads((self.path / "current.json").read_text(encoding="utf-8")) if generation is None else None
        self._install(self._read_generation(generation or pointer["generation"], pointer))

    def rollback(self, generation: str) -> None:
        state = self._read_generation(generation)
        envelope = json.loads((self.path / "generations" / f"{generation}.json").read_text(encoding="utf-8"))
        # Prepare matrices/providers before acquiring the shared writer lock.
        indexes = {view: ExactVectorIndex(data["space_id"], data["dimension"], data["rows"]) for view, data in state["indexes"].items()}
        providers = {view: LocalTfidfEmbeddingProvider.from_state(data) if data["type"] == "local_tfidf" else self.provider for view, data in state["providers"].items()}
        with self.store._writer():
            _atomic_write(self.path / "current.json", _json(self._pointer(state, envelope["sha256"])))
            self._install(state, indexes=indexes, providers=providers)

    def query(self, text: str, view: str = "event", limit: int = 5, space_id: str | None = None,
              *, _snapshot: dict | None = None, _allowed_refs: set[str] | None = None) -> list[dict]:
        installed = _snapshot if _snapshot is not None else self.capture_state()
        if view not in installed["indexes"]:
            if space_id is not None:
                raise ValueError("Requested embedding view/space is unavailable")
            return []
        index, provider = installed["indexes"][view], installed["providers"].get(view)
        selected_space = space_id or index.space_id
        if selected_space != index.space_id:
            raise ValueError("Query requested a different vector space")
        if isinstance(provider, LocalTfidfEmbeddingProvider):
            batch = provider.transform([text], selected_space)
        elif provider is not None and callable(getattr(provider, "embed_sync", None)):
            batch = provider.embed_sync([text], selected_space)
        else:
            raise RuntimeError("Async embedding provider requires await index.aquery(...) with its configured provider")
        metadata = index.rows[0]
        if (batch.model, batch.model_revision, batch.dimension, batch.normalization) != (metadata["model"], metadata["model_revision"], metadata["dimension"], metadata["normalization"]):
            raise ValueError("Query provider has migrated; rebuild or use the saved model revision")
        return index.search(batch.vectors[0], space_id=batch.space_id, limit=limit, allowed_refs=_allowed_refs)

    async def aquery(self, text: str, view: str = "event", limit: int = 5, space_id: str | None = None) -> list[dict]:
        installed = self.capture_state()
        if view not in installed["indexes"]:
            if space_id is not None:
                raise ValueError("Requested embedding view/space is unavailable")
            return []
        index, provider = installed["indexes"][view], installed["providers"].get(view)
        if provider is None:
            raise RuntimeError("Reloading an external generation requires its provider")
        selected_space = space_id or index.space_id
        if selected_space != index.space_id:
            raise ValueError("Query requested a different vector space")
        batch = await provider.embed([text], selected_space)
        metadata = index.rows[0]
        if (batch.model, batch.model_revision, batch.dimension, batch.normalization) != (metadata["model"], metadata["model_revision"], metadata["dimension"], metadata["normalization"]):
            raise ValueError("Query provider has migrated; rebuild or use the saved model revision")
        return await asyncio.to_thread(index.search, batch.vectors[0], space_id=batch.space_id, limit=limit)

    def theme_materials(self, theme_id: str) -> dict:
        installed = self.capture_state()
        records = {r.ref: r for r in self.store.list_records(snapshot=installed["watermark"])}
        return theme_materials(installed["themes"][theme_id], records, installed["graph"])

    def inspect(self) -> dict:
        installed = self.capture_state()
        return {"generation": installed["generation"], "watermark": installed["watermark"],
                "spaces": {view: {"space_id": index.space_id, "dimension": index.dimension, "count": len(index.rows)} for view, index in installed["indexes"].items()},
                "graph": installed["graph"].state(), "clusters": installed["clusters"], "themes": installed["themes"],
                "cache_hits": installed["cache_hits"], "limitations": installed["limitations"]}
