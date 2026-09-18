"""The vector store — Qdrant, embedded.

Why a vector database rather than a list of vectors
---------------------------------------------------
For a few thousand chunks you could keep the vectors in a list and compare the
query against every one. That is exact, and it is what a naive implementation
does. It stops being reasonable when the collection grows, and it gives you
nothing else: no persistence, no metadata, no filtering.

A vector database adds three things:

1. **Approximate nearest-neighbour search.** Instead of comparing against every
   vector, it navigates an index (HNSW — a graph where each vector links to its
   neighbours) and finds the closest few by walking it. Sub-linear rather than
   linear, at the cost of occasionally missing a true nearest neighbour.
2. **Persistence.** Index once, query many times, across processes.
3. **Metadata with filters** — which for this project is the important one.

Why filtering is the deciding feature
-------------------------------------
Retrieval here is never purely semantic. Every real query is::

    chunks from THIS repository,
    that are source code (not tests),
    in Python,
    similar to this issue description

There are two ways to do that, and they are not equivalent:

* **Post-filtering** — search for 50, throw away the ones that do not match.
  If 45 of the 50 nearest chunks are test files, you asked for 10 results and
  get 5. Recall silently drops, and the number you report is wrong.
* **Pre-filtering** — narrow to matching chunks *first*, then search within
  them. You get the 10 best results that satisfy the filter.

Qdrant does the second, using payload indexes, which is why the fields we filter
on are indexed explicitly below.

Why embedded mode
-----------------
``QdrantClient(path=...)`` runs Qdrant as a library writing to a local folder —
no server, no Docker, no extra gigabyte of RAM on an 8 GB laptop. Crucially the
API is identical to the server client, so "how would you scale this?" is
answered by changing a constructor argument rather than rewriting retrieval.
See [ADR-011](../../../docs/adr/ADR-011-qdrant.md).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict
from qdrant_client import QdrantClient, models

from patchpilot.logging import get_logger
from patchpilot.rag.chunking import Chunk, ChunkKind

log = get_logger("rag.store")

RetrievalSource = Literal["dense", "sparse", "hybrid", "rerank"]


class ScoredChunk(BaseModel):
    """A chunk with a relevance score and where the score came from."""

    model_config = ConfigDict(frozen=True)

    chunk: Chunk
    score: float
    source: RetrievalSource = "dense"

    def __str__(self) -> str:
        return f"[{self.score:.3f} {self.source}] {self.chunk.location} {self.chunk.qualified_symbol}"


class VectorStore:
    """An embedded Qdrant collection of chunks."""

    def __init__(
        self,
        *,
        dimension: int,
        path: Path | None = None,
        collection: str = "patchpilot",
    ) -> None:
        # `path=None` gives an in-memory instance — used by tests, and it
        # exercises the same code paths as the on-disk one.
        self._client = QdrantClient(path=str(path)) if path else QdrantClient(":memory:")
        self._collection = collection
        self._dimension = dimension
        # Embedded mode. Kept as a flag because one behaviour genuinely differs
        # (payload indexes), and because a server client would set it False.
        self._is_local = True
        self._ensure_collection()

    @property
    def collection(self) -> str:
        return self._collection

    def close(self) -> None:
        self._client.close()

    def _ensure_collection(self) -> None:
        if self._client.collection_exists(self._collection):
            return

        self._client.create_collection(
            collection_name=self._collection,
            vectors_config=models.VectorParams(
                size=self._dimension,
                # Cosine because our vectors are normalised and we care about
                # direction (meaning), not magnitude (length of the text).
                distance=models.Distance.COSINE,
            ),
        )

        # Payload indexes make filtered search fast on a Qdrant *server*. In
        # embedded mode they are accepted and ignored — the client says so with
        # a warning — so creating them here would be noise that implies a
        # guarantee we do not have.
        #
        # What still holds in embedded mode, and it is the part that matters:
        # filtering is applied *during* the search, not after it. Verified
        # directly — store 30 test chunks that match a query well plus 4 source
        # chunks that match it poorly, then ask for 4 results excluding tests.
        # Post-filtering would return fewer than 4 (top-4 overall, then
        # discard); embedded Qdrant returns 4 source chunks. So recall under a
        # filter is correct here; only the index acceleration is missing, and
        # that is a scale concern rather than a correctness one.
        # See docs/metrics.md, "Filtering behaviour, verified".
        if not self._is_local:
            for field in ("repository", "file_path", "kind", "language", "is_test"):
                self._client.create_payload_index(
                    collection_name=self._collection,
                    field_name=field,
                    field_schema=(
                        models.PayloadSchemaType.BOOL
                        if field == "is_test"
                        else models.PayloadSchemaType.KEYWORD
                    ),
                )

        log.info("collection_created", collection=self._collection, dimension=self._dimension)

    # --- writing -----------------------------------------------------------

    def upsert(self, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
        """Insert or replace chunks.

        Upsert rather than insert, because chunk ids are derived from location
        and content — so re-indexing an unchanged file writes the same ids and
        overwrites rather than duplicating. Indexing is therefore idempotent,
        and a crashed run can simply be re-run.
        """
        if len(chunks) != len(vectors):
            raise ValueError(f"{len(chunks)} chunks but {len(vectors)} vectors")
        if not chunks:
            return 0

        points = [
            models.PointStruct(
                # Qdrant ids must be UUIDs or unsigned integers. Chunk ids are
                # 32 hex characters, which is exactly a UUID.
                id=str(uuid.UUID(hex=chunk.id)),
                vector=list(vector),
                payload=chunk.model_dump(mode="json"),
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        self._client.upsert(collection_name=self._collection, points=points, wait=True)
        log.debug("chunks_upserted", count=len(points), collection=self._collection)
        return len(points)

    def delete_repository(self, repository: str) -> None:
        """Remove one repository's chunks, leaving others untouched."""
        selector = _build_filter(repository=repository)
        if selector is None:  # pragma: no cover - repository is always provided
            raise ValueError("refusing to delete without a repository filter")
        self._client.delete(
            collection_name=self._collection,
            points_selector=models.FilterSelector(filter=selector),
            wait=True,
        )

    # --- reading -----------------------------------------------------------

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 10,
        repository: str | None = None,
        exclude_tests: bool = False,
        kinds: Sequence[ChunkKind] | None = None,
        language: str | None = None,
        file_path: str | None = None,
    ) -> list[ScoredChunk]:
        """Nearest chunks, narrowed by metadata *before* the vector search."""
        results = self._client.query_points(
            collection_name=self._collection,
            query=list(vector),
            limit=limit,
            query_filter=_build_filter(
                repository=repository,
                exclude_tests=exclude_tests,
                kinds=kinds,
                language=language,
                file_path=file_path,
            ),
            with_payload=True,
        ).points

        return [
            ScoredChunk(chunk=Chunk(**point.payload), score=point.score, source="dense")
            for point in results
            if point.payload
        ]

    def all_chunks(self, repository: str | None = None, *, limit: int = 100_000) -> list[Chunk]:
        """Every stored chunk.

        Needed because BM25 is computed in-process over the whole corpus — see
        :mod:`patchpilot.rag.retrieval`. That is a deliberate simplification
        with a known scaling limit, recorded there rather than hidden.
        """
        chunks: list[Chunk] = []
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection_name=self._collection,
                scroll_filter=_build_filter(repository=repository),
                limit=min(1000, limit - len(chunks)),
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            chunks.extend(Chunk(**p.payload) for p in points if p.payload)
            if offset is None or len(chunks) >= limit:
                return chunks

    def count(self, repository: str | None = None) -> int:
        return self._client.count(
            collection_name=self._collection,
            count_filter=_build_filter(repository=repository),
            exact=True,
        ).count


def _build_filter(
    *,
    repository: str | None = None,
    exclude_tests: bool = False,
    kinds: Sequence[ChunkKind] | None = None,
    language: str | None = None,
    file_path: str | None = None,
) -> models.Filter | None:
    """Translate our vocabulary into Qdrant's filter shape.

    Kept in one function so that the rest of the codebase never constructs a
    Qdrant object — which is what keeps swapping the store a contained change.
    """
    must: list[Any] = []

    if repository:
        must.append(models.FieldCondition(key="repository", match=models.MatchValue(value=repository)))
    if exclude_tests:
        must.append(models.FieldCondition(key="is_test", match=models.MatchValue(value=False)))
    if kinds:
        must.append(models.FieldCondition(key="kind", match=models.MatchAny(any=[str(k) for k in kinds])))
    if language:
        must.append(models.FieldCondition(key="language", match=models.MatchValue(value=language)))
    if file_path:
        must.append(models.FieldCondition(key="file_path", match=models.MatchValue(value=file_path)))

    return models.Filter(must=must) if must else None
