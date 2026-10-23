"""Validated exact cosine baseline with explicit embedding-space identity."""
from __future__ import annotations

import numpy as np
import heapq


class ExactVectorIndex:
    def __init__(self, space_id: str, dimension: int, rows: list[dict] | None = None):
        self.space_id, self.dimension = space_id, dimension
        self.rows: list[dict] = []
        self.matrix = np.empty((0, dimension), dtype=float)
        if rows:
            self.replace(rows)

    def replace(self, rows: list[dict]) -> None:
        for row in rows:
            if row["space_id"] != self.space_id or len(row["vector"]) != self.dimension:
                raise ValueError("Embedding space/dimension mismatch")
        matrix = np.asarray([r["vector"] for r in rows], dtype=float).reshape((-1, self.dimension))
        if not np.isfinite(matrix).all():
            raise ValueError("Nonfinite vector")
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        self.matrix = np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms != 0)
        self.rows = rows

    def search(self, vector: list[float], *, space_id: str, limit: int = 5, exclude_ref: str | None = None,
               allowed_refs: set[str] | None = None) -> list[dict]:
        if space_id != self.space_id or len(vector) != self.dimension:
            raise ValueError("Query vector space/dimension mismatch")
        query = np.asarray(vector, dtype=float)
        if not np.isfinite(query).all():
            raise ValueError("Nonfinite query")
        norm = np.linalg.norm(query)
        if norm == 0 or not self.rows or limit <= 0:
            return []
        scores = self.matrix @ (query / norm)
        indices = heapq.nsmallest(limit, (i for i in range(len(scores)) if scores[i] > 0
            and self.rows[i]["ref"] != exclude_ref and (allowed_refs is None or self.rows[i]["ref"] in allowed_refs)),
            key=lambda i: (-float(scores[i]), self.rows[i]["ref"]))
        return [{"ref": self.rows[i]["ref"], "score": float(np.clip(scores[i], -1, 1)), "space_id": self.space_id,
                 "view": self.rows[i]["view"]} for i in indices if self.rows[i]["ref"] != exclude_ref and scores[i] > 0][:limit]
