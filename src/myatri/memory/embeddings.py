"""Real, fully offline lexical embedding baseline and embedding cache keys.

The default is fitted character TF-IDF, not a neural semantic model. A fit is a
new vector space: equal dimensionality never authorizes mixing two fits.
"""
from __future__ import annotations

import asyncio
import hashlib
from typing import Any

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer

from myatri.contracts import EmbeddingBatch, digest


class LocalTfidfEmbeddingProvider:
    model = "sklearn-char-tfidf"
    normalization = "l2"

    def __init__(self, view: str = "event"):
        self.view = view
        self.vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(1, 4), lowercase=True, norm="l2", sublinear_tf=True)
        self.space_id = ""
        self.model_revision = ""
        self.dimension = 0

    def fit(self, texts: list[str]) -> "LocalTfidfEmbeddingProvider":
        if not texts or not any(text.strip() for text in texts):
            raise ValueError("Cannot fit TF-IDF without nonempty text")
        self.vectorizer.fit(texts)
        self.dimension = len(self.vectorizer.vocabulary_)
        self.model_revision = digest({"corpus": texts, "view": self.view, "algorithm": "char1-4-sublinear-v1"})
        self.space_id = f"tfidf_{self.view}_{self.model_revision[:20]}"
        return self

    def transform(self, texts: list[str], space_id: str | None = None) -> EmbeddingBatch:
        if not self.space_id:
            raise ValueError("TF-IDF provider must be fitted before use")
        if space_id is not None and space_id != self.space_id:
            raise ValueError("Vector space mismatch; migrate or restore the matching provider")
        vectors = self.vectorizer.transform(texts).toarray().tolist()
        return EmbeddingBatch(vectors=vectors, space_id=self.space_id, model=self.model,
                              model_revision=self.model_revision, dimension=self.dimension, normalization="l2")

    async def embed(self, texts: list[str], space_id: str) -> EmbeddingBatch:
        return await asyncio.to_thread(self.transform, texts, space_id)

    def state(self) -> dict[str, Any]:
        return {"type": "local_tfidf", "view": self.view, "space_id": self.space_id,
                "model_revision": self.model_revision, "dimension": self.dimension,
                "vocabulary": self.vectorizer.vocabulary_, "idf": self.vectorizer.idf_.tolist()}

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "LocalTfidfEmbeddingProvider":
        provider = cls(state["view"])
        provider.space_id, provider.model_revision, provider.dimension = state["space_id"], state["model_revision"], state["dimension"]
        vocabulary = state["vocabulary"]
        provider.vectorizer = TfidfVectorizer(analyzer="char", ngram_range=(1, 4), lowercase=True, norm="l2", sublinear_tf=True, vocabulary=vocabulary)
        # A tiny transform initializes sklearn's validated transformer; only
        # primitive vocabulary/idf data is persisted (never executable pickle).
        provider.vectorizer.fit(["初始化"])
        provider.vectorizer.idf_ = np.asarray(state["idf"], dtype=float)
        return provider


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cache_key(text: str, view: str, batch: EmbeddingBatch) -> str:
    return digest({"text_hash": text_hash(text), "view": view, "space_id": batch.space_id,
                   "model": batch.model, "revision": batch.model_revision,
                   "dimension": batch.dimension, "normalization": batch.normalization})
