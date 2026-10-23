"""Stable provisional theme identities, split/merge lineage and soft membership.

This module computes retrieval structure. It does not supply model-generated
semantic names or infer personality from cluster membership.
"""
from __future__ import annotations

from myatri.contracts import ClusterSnapshot, Record, digest
from .graph import MemoryGraph


def build_themes(snapshot: ClusterSnapshot, graph: MemoryGraph, records: dict[str, Record], previous: dict[str, dict] | None = None) -> tuple[dict[str, dict], list[dict]]:
    previous = previous or {}
    groups = {key: set(snapshot.members[key]) for key in snapshot.quality["leaf_cluster_ids"]}
    overlaps = []
    for old_id, old in previous.items():
        before = set(old["members"])
        for cluster_id, after in groups.items():
            shared = before & after
            if shared:
                overlaps.append((len(shared) / len(before | after), old_id, cluster_id))
    assigned: dict[str, str] = {}
    used_old: set[str] = set()
    for score, old_id, cluster_id in sorted(overlaps, reverse=True):
        if score >= 0.25 and old_id not in used_old and cluster_id not in assigned:
            assigned[cluster_id] = old_id
            used_old.add(old_id)
    themes: dict[str, dict] = {}
    cluster_theme: dict[str, str] = {}
    for index, (cluster_id, group) in enumerate(sorted(groups.items())):
        theme_id = assigned.get(cluster_id, f"theme_{digest(sorted(group))[:20]}")
        cluster_theme[cluster_id] = theme_id
        old = previous.get(theme_id, {})
        semantic_material = sorted({digest({key: edge.get(key) for key in ("type", "from_ref", "to_ref", "evidence_refs", "status")})
                                    for edge in graph.edges if edge["type"] != "similar" and
                                    (edge["from_ref"] in group or edge["to_ref"] in group)})
        fingerprint = digest({"members": [[ref, records[ref].content_hash] for ref in sorted(group)], "semantic_edges": semantic_material})
        unchanged = fingerprint == old.get("material_hash")
        themes[theme_id] = {"theme_id": theme_id, "cluster_id": cluster_id, "members": sorted(group),
                            "name": old.get("name", f"暂定主题 {index + 1}") if unchanged else f"暂定主题 {index + 1}", "status": "provisional",
                            "summary": old.get("summary", "") if unchanged else "",
                            "material_hash": fingerprint, "needs_summary": old.get("needs_summary", True) if unchanged else True,
                            "naming_source": old.get("naming_source", "algorithmic_placeholder") if unchanged else "algorithmic_placeholder",
                            "memberships": {ref: 1.0 for ref in group}}
    lineage = []
    for old_id, old in previous.items():
        targets = [cluster_theme[cid] for _, oid, cid in overlaps if oid == old_id]
        targets = sorted(set(targets))
        relation = "retired" if not targets else "split" if len(targets) > 1 else "continued"
        lineage.append({"type": relation, "from": [old_id], "to": targets})
    for cluster_id, theme_id in cluster_theme.items():
        origins = sorted({oid for _, oid, cid in overlaps if cid == cluster_id})
        if len(origins) > 1:
            lineage.append({"type": "merged", "from": origins, "to": [theme_id]})
        elif not origins:
            lineage.append({"type": "created", "from": [], "to": [theme_id]})
    # A node can be softly associated with a second theme through positive
    # similarity links. These strengths are retrieval weights, not probabilities.
    for ref in graph.nodes:
        strengths: dict[str, float] = {}
        for edge in graph.edges:
            if edge["type"] != "similar":
                continue
            other = edge["to_ref"] if edge["from_ref"] == ref else edge["from_ref"] if edge["to_ref"] == ref else None
            if other is None:
                continue
            for theme_id, theme in themes.items():
                if other in theme["members"]:
                    strengths[theme_id] = strengths.get(theme_id, 0) + edge["weight"]
        total = sum(strengths.values())
        if total:
            for theme_id, strength in strengths.items():
                weight = strength / total
                if weight >= 0.15:
                    themes[theme_id]["memberships"][ref] = max(themes[theme_id]["memberships"].get(ref, 0), weight)
    return themes, lineage


def theme_materials(theme: dict, records: dict[str, Record], graph: MemoryGraph) -> dict:
    members = [records[ref] for ref in theme["members"] if ref in records]
    scored = sorted(members, key=lambda record: (-sum(e["weight"] for e in graph.edges if e["type"] == "similar" and record.ref in (e["from_ref"], e["to_ref"])), record.ref))
    representatives = [r.ref for r in scored[:3]]
    differences = [r.ref for r in sorted(members, key=lambda r: r.created_at)[-2:] if r.ref not in representatives]
    counter_refs = sorted({endpoint for edge in graph.edges if edge["type"] == "contradicts" and (edge["from_ref"] in theme["members"] or edge["to_ref"] in theme["members"])
                           for endpoint in (edge["from_ref"], edge["to_ref"])})
    return {"theme_id": theme["theme_id"], "material_hash": theme["material_hash"], "representative_refs": representatives,
            "difference_refs": differences, "counter_refs": counter_refs, "source_refs": [r.ref for r in members],
            "time_span": [min((r.created_at for r in members), default=None), max((r.created_at for r in members), default=None)],
            "previous_summary": theme.get("summary", ""), "needs_summary": theme["needs_summary"]}
