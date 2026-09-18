"""Turning text into vectors.

What an embedding is
--------------------
An embedding model maps a piece of text to a list of numbers — here, 384 of
them. The numbers are arranged so that *texts with similar meaning land near
each other*. That is the entire trick, and everything about vector search
follows from it.

"Near" means the angle between the two vectors is small. Since our vectors are
normalised to length 1, that reduces to the **dot product**, and to cosine
similarity::

    similar meaning   →  cos ≈ 1.0
    unrelated         →  cos ≈ 0.0
    opposite          →  cos ≈ -1.0

Why this beats keyword search, and where it does not
----------------------------------------------------
A bug report says *"crashes when the table doesn't exist"*. The code says
``if not self.exists(): raise NoTable``. No shared words — a keyword search
finds nothing. Embeddings find it, because the *meanings* are close.

The reverse also happens. Search for ``rows_where`` and an embedding model
returns things that are semantically about querying rows, which may not include
the function literally called ``rows_where``. Exact identifiers are where
keyword search wins outright.

Neither is sufficient, which is why retrieval here is
[hybrid](../../../docs/concepts/bm25-and-hybrid-search.md).

Why the model runs locally
--------------------------
It is 67 MB and runs on the CPU. Indexing a repository means embedding thousands
of chunks; on a hosted API that is thousands of billed calls, and on a free tier
it is a quota we do not have. Locally it is free and works offline — which also
means the tests can use the real thing.

Asymmetry: queries and documents are embedded differently
---------------------------------------------------------
BGE models are trained so that a *question* and the *passage answering it* end
up close together. To get that, the question needs a short instruction prefix
and the passage does not. Embedding a query as though it were a document is a
subtle, silent quality loss — the code still runs, results are just worse. The
model exposes ``query_embed`` and ``passage_embed`` for exactly this, and this
module keeps them distinct.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from patchpilot.logging import get_logger

log = get_logger("rag.embeddings")

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_DIMENSION = 384


@runtime_checkable
class EmbeddingModel(Protocol):
    """What retrieval needs from an embedding model."""

    @property
    def name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed passages for storage."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query. Not the same operation as above."""
        ...


class LocalEmbeddings:
    """fastembed running BGE-small on the CPU.

    The model is downloaded once (~67 MB) and cached. Loading is deferred to
    first use so that importing this module — which the whole test suite does —
    does not pull a model off disk.
    """

    def __init__(
        self, model_name: str = DEFAULT_MODEL, *, dimension: int = DEFAULT_DIMENSION
    ) -> None:
        self._model_name = model_name
        self._dimension = dimension
        self._model: object | None = None

    @property
    def name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def _load(self) -> object:
        if self._model is None:
            from fastembed import TextEmbedding

            log.info("embedding_model_loading", model=self._model_name)
            self._model = TextEmbedding(model_name=self._model_name)
        return self._model

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        model = self._load()
        return [list(map(float, v)) for v in model.passage_embed(list(texts))]  # type: ignore[attr-defined]

    def embed_query(self, text: str) -> list[float]:
        model = self._load()
        return [float(x) for x in next(iter(model.query_embed([text])))]  # type: ignore[attr-defined]


TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class HashingEmbeddings:
    """A deterministic, dependency-free embedding for tests.

    Not a stub that returns zeros. It is a *hashing vectoriser*: tokenise, hash
    each token into one of ``dimension`` buckets, count, then normalise. Texts
    sharing vocabulary genuinely come out close together.

    That matters — a fake returning random vectors would let a broken retrieval
    pipeline pass its tests. This one gives real (if crude, lexical) similarity,
    so the tests exercise the actual ranking behaviour while staying offline and
    instant.

    It cannot do what a real embedding does: match "crashes when the table is
    missing" to ``raise NoTable``. That is what the real model is for, and what
    the Phase 3 retrieval benchmark measures.
    """

    def __init__(self, dimension: int = 64) -> None:
        self._dimension = dimension

    @property
    def name(self) -> str:
        return f"hashing/{self._dimension}"

    @property
    def dimension(self) -> int:
        return self._dimension

    def _vector(self, text: str) -> list[float]:
        buckets = [0.0] * self._dimension
        for token in TOKEN.findall(text.lower()):
            index = int(hashlib.md5(token.encode()).hexdigest(), 16) % self._dimension
            buckets[index] += 1.0

        norm = math.sqrt(sum(v * v for v in buckets))
        return [v / norm for v in buckets] if norm else buckets

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, written out rather than imported.

    For normalised vectors this is just the dot product. It is here mostly so
    the tests can assert on it directly, and so the arithmetic behind "vector
    search" is visible in the repository rather than hidden in a library.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    return 0.0 if norm_a == 0 or norm_b == 0 else dot / (norm_a * norm_b)
