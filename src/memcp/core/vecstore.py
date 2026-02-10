"""Vector store — numpy .npz-based storage for embeddings.

Stores embeddings alongside chunks on disk:
    ~/.memcp/chunks/{context_name}/embeddings.npz

For memory insights:
    ~/.memcp/cache/insight_embeddings.npz

Brute-force cosine similarity search (sufficient for <100K vectors).
All numpy operations are guarded — module degrades gracefully if numpy is absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

NUMPY_AVAILABLE = False
try:
    import numpy as np

    NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore[assignment]


class VectorStore:
    """Numpy-based vector store for chunk/insight embeddings.

    Stores vectors in a .npz file with two arrays:
    - ids: 1D array of ID strings
    - vectors: 2D array of float32 vectors

    Search uses brute-force cosine similarity.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.ids: list[str] = []
        self.vectors: Any = None  # np.ndarray | None

    def load(self) -> bool:
        """Load vectors from .npz file. Returns True on success."""
        if not NUMPY_AVAILABLE or not self.path.exists():
            return False
        try:
            data = np.load(self.path, allow_pickle=True)
            self.ids = list(data["ids"])
            self.vectors = data["vectors"].astype(np.float32)
            return True
        except Exception:
            self.ids = []
            self.vectors = None
            return False

    def save(self) -> None:
        """Persist vectors atomically to .npz file."""
        if not NUMPY_AVAILABLE or self.vectors is None or len(self.ids) == 0:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.parent / (self.path.stem + "_tmp.npz")
        try:
            np.savez(
                tmp_path,
                ids=np.array(self.ids, dtype=object),
                vectors=self.vectors.astype(np.float32),
            )
            tmp_path.rename(self.path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    def add(self, item_id: str, vector: list[float]) -> None:
        """Add or replace a vector for an item."""
        if not NUMPY_AVAILABLE:
            return
        vec = np.asarray(vector, dtype=np.float32).reshape(1, -1)
        if item_id in self.ids:
            idx = self.ids.index(item_id)
            self.vectors[idx] = vec[0]
            return
        self.ids.append(item_id)
        if self.vectors is None:
            self.vectors = vec
        else:
            self.vectors = np.vstack([self.vectors, vec])

    def add_batch(self, item_ids: list[str], vectors: list[list[float]]) -> None:
        """Add multiple vectors at once (more efficient than repeated add)."""
        if not NUMPY_AVAILABLE or not item_ids:
            return
        new_vecs = np.asarray(vectors, dtype=np.float32)
        for i, item_id in enumerate(item_ids):
            if item_id in self.ids:
                idx = self.ids.index(item_id)
                self.vectors[idx] = new_vecs[i]
            else:
                self.ids.append(item_id)
                if self.vectors is None:
                    self.vectors = new_vecs[i : i + 1]
                else:
                    self.vectors = np.vstack([self.vectors, new_vecs[i : i + 1]])

    def remove(self, item_id: str) -> bool:
        """Remove a vector by ID. Returns True if found."""
        if not NUMPY_AVAILABLE or item_id not in self.ids:
            return False
        idx = self.ids.index(item_id)
        self.ids.pop(idx)
        if self.vectors is not None:
            self.vectors = np.delete(self.vectors, idx, axis=0)
            if len(self.ids) == 0:
                self.vectors = None
        return True

    def search(self, query_vector: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        """Cosine similarity search. Returns list of (id, score) tuples."""
        if not NUMPY_AVAILABLE or self.vectors is None or len(self.ids) == 0:
            return []
        query = np.asarray(query_vector, dtype=np.float32).reshape(1, -1)
        q_norm = float(np.linalg.norm(query))
        if q_norm == 0:
            return []
        v_norms = np.linalg.norm(self.vectors, axis=1)
        v_norms = np.where(v_norms == 0, 1e-10, v_norms)
        similarities = (self.vectors @ query.T).flatten() / (v_norms * q_norm)
        similarities = np.clip(similarities, 0, 1)
        k = min(top_k, len(self.ids))
        top_indices = np.argsort(similarities)[::-1][:k]
        results = []
        for idx in top_indices:
            score = float(similarities[idx])
            if score > 0:
                results.append((self.ids[idx], score))
        return results

    def count(self) -> int:
        """Number of stored vectors."""
        return len(self.ids)

    def has_id(self, item_id: str) -> bool:
        """Check if an ID exists in the store."""
        return item_id in self.ids
