"""Actual Leiden runs with recursive, explicitly recorded resolution levels."""
from __future__ import annotations

from importlib.metadata import version

import igraph as ig
import leidenalg

from myatri.contracts import ClusterSnapshot, digest
from .graph import MemoryGraph


def cluster(graph: MemoryGraph, *, seed: int = 17, resolutions: tuple[float, ...] = (0.5, 1.25)) -> ClusterSnapshot:
    if not resolutions or any(r <= 0 for r in resolutions):
        raise ValueError("Positive clustering resolutions are required")
    nodes = graph.nodes
    positive = [e for e in graph.edges if e["type"] == "similar" and e["weight"] > 0]
    members: dict[str, list[str]] = {}
    parent: dict[str, str] = {}
    levels: dict[str, int] = {}
    partitions: list[tuple[str | None, list[str]]] = [(None, nodes)] if nodes else []
    runs = 0
    for level, resolution in enumerate(resolutions):
        next_partitions = []
        for parent_id, subset in partitions:
            positions = {ref: i for i, ref in enumerate(subset)}
            edges = [e for e in positive if e["from_ref"] in positions and e["to_ref"] in positions]
            if edges:
                network = ig.Graph(n=len(subset), edges=[(positions[e["from_ref"]], positions[e["to_ref"]]) for e in edges], directed=False)
                partition = leidenalg.find_partition(network, leidenalg.RBConfigurationVertexPartition,
                                                     weights=[e["weight"] for e in edges], resolution_parameter=resolution, seed=seed)
                groups = [[subset[i] for i in group] for group in partition]
                runs += 1
            else:
                groups = [[ref] for ref in subset]
            for group in sorted((sorted(g) for g in groups), key=lambda g: g[0]):
                cluster_id = f"l{level}_{digest(group)[:16]}"
                members[cluster_id] = group
                levels[cluster_id] = level
                if parent_id is not None:
                    parent[cluster_id] = parent_id
                next_partitions.append((cluster_id, group))
        partitions = next_partitions
    isolates = [ref for ref in nodes if not any(ref in (e["from_ref"], e["to_ref"]) for e in positive)]
    params = {"resolutions": list(resolutions), "method": "recursive_subgraph", "edge_type": "similar", "partition": "RBConfigurationVertexPartition"}
    run_id = f"cluster_{digest({'graph':graph.state(),'params':params,'seed':seed})[:24]}"
    return ClusterSnapshot(cluster_run_id=run_id, graph_generation=graph.generation, algorithm_version=version("leidenalg"),
                           params=params, seed=seed, members=members, parent=parent,
                           quality={"levels": levels, "leaf_cluster_ids": [p[0] for p in partitions], "isolated_nodes": isolates,
                                    "actual_leiden_runs": runs, "provisional": True, "overlap": "separate soft memberships; each Leiden level is a partition"})
