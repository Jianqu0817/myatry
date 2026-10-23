"""Sparse similarity graph and separately typed, evidence-backed semantic edges."""
from __future__ import annotations

from myatri._vendor.amem import iter_unique_neighbors
from myatri.contracts import Edge, Record, digest
from .vector_index import ExactVectorIndex


class MemoryGraph:
    def __init__(self, nodes: list[str] | None = None, edges: list[dict] | None = None, generation: str = ""):
        self.nodes = sorted(set(nodes or []))
        self.edges = list(edges or [])
        self.generation = generation

    def upsert_similarity_edges(self, indexes: dict[str, ExactVectorIndex], *, k: int = 5, threshold: float = 0.08,
                                view_weights: dict[str, float] | None = None, mutual: bool = False) -> None:
        weights = view_weights or {"event": 0.7, "feeling": 0.3}
        directed: dict[tuple[str, str], dict[str, float]] = {}
        nodes = set(self.nodes)
        for view, index in indexes.items():
            if view not in weights:
                continue
            anchors = {r["ref"]: r.get("event_ref", r["ref"]) for r in index.rows}
            for row in index.rows:
                origin = anchors[row["ref"]]
                if origin not in nodes:
                    continue
                hits = index.search(row["vector"], space_id=index.space_id, limit=len(index.rows), exclude_ref=row["ref"])
                valid = [h for h in hits if anchors[h["ref"]] in nodes and anchors[h["ref"]] != origin]
                for hit in valid[:k]:
                    directed.setdefault((origin, anchors[hit["ref"]]), {})[view] = hit["score"]
        pairs: dict[tuple[str, str], dict[str, float]] = {}
        for (left, right), scores in directed.items():
            if mutual and (right, left) not in directed:
                continue
            pair = tuple(sorted((left, right)))
            target = pairs.setdefault(pair, {})
            for view, score in scores.items():
                target[view] = max(target.get(view, 0), score)
        candidates = []
        for (left, right), scores in pairs.items():
            # Missing views are omitted and remaining weights renormalized.
            denominator = sum(weights[v] for v in scores)
            score = sum(weights[v] * s for v, s in scores.items()) / denominator
            if score >= threshold:
                candidates.append({"edge_id": f"sim_{digest([left,right])[:24]}", "from_ref": left, "to_ref": right,
                                   "type": "similar", "direction": "undirected", "weight": score,
                                   "views": scores, "missing_view_policy": "renormalize_available", "status": "algorithmic_candidate"})
        # Union kNN can create hubs; cap final degree explicitly.
        degree = {n: 0 for n in nodes}
        selected = []
        for edge in sorted(candidates, key=lambda e: (-e["weight"], e["edge_id"])):
            if degree[edge["from_ref"]] < k and degree[edge["to_ref"]] < k:
                selected.append(edge)
                degree[edge["from_ref"]] += 1
                degree[edge["to_ref"]] += 1
        self.edges = [e for e in self.edges if e["type"] != "similar"] + selected

    def apply_semantic_edges(self, records: list[Record], available_refs: set[str]) -> None:
        by_id = {e["edge_id"]: e for e in self.edges if e["type"] == "similar"}
        for record in records:
            edge = Edge.model_validate(record.payload)
            if edge.type == "similar":
                raise ValueError("Semantic proposals may not masquerade as similarity algorithm output")
            if edge.from_ref not in available_refs or edge.to_ref not in available_refs:
                raise ValueError("Semantic edge endpoint missing in current snapshot")
            if not edge.evidence_refs or any(ref not in available_refs for ref in edge.evidence_refs):
                raise ValueError("Semantic edges require existing evidence")
            if not set(edge.evidence_refs).issubset(record.source_refs):
                raise ValueError("Semantic edge evidence must also be pinned in its record sources")
            data = edge.model_dump(mode="json")
            data["record_ref"] = record.ref
            by_id[edge.edge_id] = data
        self.edges = list(by_id.values())

    def get_subgraph(self, seeds: list[str], *, depth: int = 1, types: set[str] | None = None, limit: int = 30) -> dict:
        accepted = types or {"follows_up", "reinterprets", "supports", "contradicts"}
        visited, frontier, edges, paths = set(seeds), list(seeds), [], [[ref] for ref in seeds]
        path_map = {ref: [ref] for ref in seeds}
        for _ in range(depth):
            next_frontier = []
            def linked_candidates():
                for edge in self.edges:
                    if edge["type"] not in accepted:
                        continue
                    left, right = edge["from_ref"], edge["to_ref"]
                    for origin, target in ((left, right), (right, left)):
                        if origin in frontier:
                            edges.append(edge)
                            yield origin, target

            for origin, target in iter_unique_neighbors(linked_candidates(), visited, limit - len(visited)):
                next_frontier.append(target)
                path_map[target] = path_map[origin] + [target]
                paths.append(path_map[target])
            frontier = next_frontier
        return {"nodes": sorted(visited), "edges": edges, "paths": paths}

    def state(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges, "generation": self.generation}
