"""Optional pinned Chinese BGE CPU inference. No implicit download at runtime."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import numpy as np

from myatri.contracts import EmbeddingBatch


MODEL = "Xenova/bge-small-zh-v1.5"
REVISION = "75c43b069aac4d136ba6bc1122f995fedcfd2781"


class LocalOnnxEmbeddingProvider:
    def __init__(self, path: Path):
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.path = Path(path)
        manifest = json.loads((self.path / "manifest.json").read_text(encoding="utf-8"))
        if manifest["model"] != MODEL or manifest["revision"] != REVISION:
            raise ValueError("unexpected local model/revision")
        for name, expected in manifest["files"].items():
            if hashlib.sha256((self.path / name).read_bytes()).hexdigest() != expected:
                raise ValueError("local model file hash mismatch")
        self.tokenizer = Tokenizer.from_file(str(self.path / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        self.tokenizer.enable_padding()
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(self.path / "model_quantized.onnx"), sess_options=options,
                                           providers=["CPUExecutionProvider"])
        self.space_id = "bge-small-zh-v1_5-" + REVISION[:12] + "-int8-cls-l2"
        self.model, self.model_revision, self.dimension = MODEL, REVISION, 512
        self.normalization = "l2"

    def _embed(self, texts, space_id):
        if space_id != self.space_id:
            raise ValueError("embedding space mismatch")
        if not texts:
            return EmbeddingBatch(vectors=[], space_id=space_id, model=MODEL, model_revision=REVISION, dimension=512)
        encodings = self.tokenizer.encode_batch(texts)
        data = {"input_ids": np.array([x.ids for x in encodings], dtype=np.int64),
                "attention_mask": np.array([x.attention_mask for x in encodings], dtype=np.int64),
                "token_type_ids": np.array([x.type_ids for x in encodings], dtype=np.int64)}
        feed = {v.name: data[v.name] for v in self.session.get_inputs()}
        output = self.session.run(None, feed)[0]
        vectors = output[:, 0, :] if output.ndim == 3 else output
        vectors = vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
        return EmbeddingBatch(vectors=vectors.tolist(), space_id=space_id, model=MODEL, model_revision=REVISION,
                              dimension=vectors.shape[1], normalization="l2")

    async def embed(self, texts, space_id):
        return await asyncio.to_thread(self._embed, texts, space_id)

    embed_sync = _embed


def download_model(path: Path):
    """Explicit user-invoked public artifact download, never called by normal chat."""
    import urllib.request
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    files = {"tokenizer.json": "tokenizer.json", "model_quantized.onnx": "onnx/model_quantized.onnx"}
    checksums = {}
    for local, remote in files.items():
        target = path / local
        if not target.exists():
            request = urllib.request.Request(f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{remote}", headers={"User-Agent": "MYATRI-local-model"})
            temp = target.with_suffix(target.suffix + ".part")
            with urllib.request.urlopen(request, timeout=60) as source, temp.open("wb") as dest:
                while block := source.read(1024 * 1024):
                    dest.write(block)
            temp.replace(target)
        checksums[local] = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = {"model": MODEL, "revision": REVISION, "files": checksums,
                "pooling": "CLS", "normalization": "l2", "format": "ONNX quantized", "dimension": 512}
    (path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
