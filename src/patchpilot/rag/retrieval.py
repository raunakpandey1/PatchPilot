"""Hybrid retrieval: vectors and keywords, fused.

Why one method is not enough
----------------------------
**Dense (vector) search** matches meaning. An issue saying *"crashes when the
table doesn't exist"* finds ``if not self.exists(): raise NoTable`` despite
sharing no words. That is exactly what keyword search cannot do.

**Keyword search (BM25)** matches exact terms. Search for ``rows_where`` and it
finds the function literally called ``rows_where``. Dense search often does not,
because an identifier is not a concept — the embedding of ``rows_where`` sits
near "things about querying rows", which includes a dozen other functions.

Code search needs both, because bug reports contain both. A typical issue has a
prose description *and* a traceback with exact symbol names. Retrieving on only
one of those throws away half the query.

BM25 in one paragraph
---------------------
"Best Match 25" scores a document against a query by asking three questions per
term: how often does the term appear in this document (more is better, with
diminishing returns); how rare is the term across the whole corpus (rare terms
are more informative — ``self`` tells you nothing, ``rows_where`` tells you a
lot); and how long is this document (a term in a short function means more than
the same term in a thousand-line file). It is not machine learning; it is a
scoring formula from the 1990s that remains extremely hard to beat on exact
terms.

Fusing two rankings that are not comparable
-------------------------------------------
Dense similarity is a cosine in [-1, 1]. BM25 is unbounded and corpus-dependent.
Adding them is meaningless, and normalising them requires knowing each one's
distribution, which changes per query.

**Reciprocal Rank Fusion** sidesteps this by discarding the scores and using
only the *positions*::

    score(chunk) = Σ  1 / (k + rank_in_that_ranking)

with ``k = 60`` by convention. A chunk ranked 1st by one method and 8th by the
other beats one ranked 3rd by both. Nothing has to be normalised, and a ranker
producing wild scores cannot dominate the result.

The constant ``k`` flattens the curve: without it, first place would be worth
twice second place, which over-weights whichever method happens to be confident.
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Literal

from rank_bm25 import BM25Okapi

from patchpilot.logging import get_logger
from patchpilot.rag.chunking import Chunk, ChunkKind
from patchpilot.rag.embeddings import EmbeddingModel
from patchpilot.rag.store import ScoredChunk, VectorStore

log = get_logger("rag.retrieval")

RetrievalMode = Literal["dense", "sparse", "hybrid"]

# The RRF constant. 60 is the value from the original paper and the usual
# default; it controls how sharply rank 1 outweighs rank 2.
RRF_K = 60

# How many candidates each method contributes before fusion. Wider than the
# final result set on purpose: a chunk ranked 15th by dense search and 2nd by
# BM25 should still be able to surface, and it cannot if dense only returned 10.
CANDIDATE_MULTIPLIER = 4

IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokenize_for_bm25(text: str) -> list[str]:
    """Split text into terms, keeping identifiers *and* their parts.

    ``rows_where`` becomes ``["rows_where", "rows", "where"]`` and
    ``getUserName`` becomes ``["getusername", "get", "user", "name"]``.

    Why both: an issue might say "the rows_where method" (exact) or "getting
    rows with a where clause" (parts). Keeping the whole identifier preserves
    the precise match; adding its parts catches the paraphrase. Keeping only the
    parts would make ``rows_where`` indistinguishable from ``where_rows``.
    """
    terms: list[str] = []
    for raw in IDENTIFIER.findall(text):
        terms.append(raw.lower())
        # Split on the *original* casing — lowercasing first destroys the
        # camelCase boundary, which silently turns `getUserName` into one term.
        parts = [p for piece in raw.split("_") for p in CAMEL_BOUNDARY.split(piece) if p]
        if len(parts) > 1:
            terms.extend(p.lower() for p in parts)
    return terms


class HybridRetriever:
    """Dense + BM25 retrieval over a :class:`VectorStore`."""

    def __init__(
        self,
        store: VectorStore,
        embeddings: EmbeddingModel,
        *,
        rrf_k: int = RRF_K,
        dense_weight: float = 1.0,
        sparse_weight: float = 1.0,
    ) -> None:
        self._store = store
        self._embeddings = embeddings
        self._rrf_k = rrf_k
        # Plain RRF weights both rankings equally, which is only right when they
        # are of comparable quality. Measured on this repository they are not —
        # see ADR-012 — so the weights are a parameter rather than an assumption.
        self._dense_weight = dense_weight
        self._sparse_weight = sparse_weight
        # BM25 is computed in-process over the whole corpus, so the index is
        # built once per repository and cached. See the scaling note below.
        self._bm25_cache: dict[str, tuple[BM25Okapi | None, list[Chunk]]] = {}

    def retrieve(
        self,
        query: str,
        *,
        limit: int = 10,
        mode: RetrievalMode = "hybrid",
        repository: str | None = None,
        exclude_tests: bool = False,
        kinds: Sequence[ChunkKind] | None = None,
        language: str | None = None,
    ) -> list[ScoredChunk]:
        """Retrieve the most relevant chunks.

        ``mode`` exists so the three strategies can be compared on the same
        benchmark. "Hybrid is better" is a claim, and Phase 3's evaluation has
        to be able to test it rather than assume it.
        """
        started = time.monotonic()
        candidates = limit * CANDIDATE_MULTIPLIER

        dense: list[ScoredChunk] = []
        sparse: list[ScoredChunk] = []

        if mode in ("dense", "hybrid"):
            dense = self._store.search(
                self._embeddings.embed_query(query),
                limit=candidates,
                repository=repository,
                exclude_tests=exclude_tests,
                kinds=kinds,
                language=language,
            )

        if mode in ("sparse", "hybrid"):
            sparse = self._bm25_search(
                query, limit=candidates, repository=repository,
                exclude_tests=exclude_tests, kinds=kinds, language=language,
            )

        if mode == "dense":
            results = dense[:limit]
        elif mode == "sparse":
            results = sparse[:limit]
        else:
            results = self._fuse(dense, sparse)[:limit]

        log.debug(
            "retrieved",
            mode=mode, query_chars=len(query), returned=len(results),
            dense=len(dense), sparse=len(sparse),
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return results

    # --- BM25 --------------------------------------------------------------

    def _bm25_index(self, repository: str | None) -> tuple[BM25Okapi | None, list[Chunk]]:
        """Build (or reuse) the keyword index for a repository.

        **Scaling limit, stated rather than hidden:** this holds the corpus in
        memory and rebuilds on demand. For one repository of a few thousand
        chunks that is milliseconds and a few megabytes. It does not survive
        many large repositories, and the fix is Qdrant's own sparse-vector
        support, which keeps the inverted index in the database. That is a
        deliberate deferral — see ADR-012.
        """
        key = repository or "__all__"
        if key not in self._bm25_cache:
            chunks = self._store.all_chunks(repository)
            corpus = [tokenize_for_bm25(c.embedding_text()) for c in chunks]
            # BM25Okapi raises on an empty corpus, and an unindexed repository
            # is a normal state, not an error.
            index = BM25Okapi(corpus) if corpus else None
            self._bm25_cache[key] = (index, chunks)
        return self._bm25_cache[key]

    def invalidate(self, repository: str | None = None) -> None:
        """Drop the cached keyword index after indexing new content."""
        if repository is None:
            self._bm25_cache.clear()
        else:
            self._bm25_cache.pop(repository, None)

    def _bm25_search(
        self,
        query: str,
        *,
        limit: int,
        repository: str | None,
        exclude_tests: bool,
        kinds: Sequence[ChunkKind] | None,
        language: str | None,
    ) -> list[ScoredChunk]:
        index, chunks = self._bm25_index(repository)
        if index is None or not chunks:
            return []

        scores = index.get_scores(tokenize_for_bm25(query))

        # Filters are applied here, before ranking — the same pre-filter
        # discipline the vector store uses. Filtering after would quietly return
        # fewer results than asked for.
        allowed = [
            (chunk, float(score))
            for chunk, score in zip(chunks, scores, strict=True)
            if score > 0 and _passes(chunk, exclude_tests, kinds, language)
        ]
        allowed.sort(key=lambda pair: -pair[1])

        return [
            ScoredChunk(chunk=chunk, score=score, source="sparse")
            for chunk, score in allowed[:limit]
        ]

    # --- fusion ------------------------------------------------------------

    def _fuse(
        self, dense: Sequence[ScoredChunk], sparse: Sequence[ScoredChunk]
    ) -> list[ScoredChunk]:
        """Weighted Reciprocal Rank Fusion of two rankings.

        Only positions are used. The underlying scores are on incomparable
        scales, and rank is the one thing both rankings agree on the meaning of.

        The weights exist because plain RRF assumes both rankings are about
        equally good. On this corpus they are not — dense retrieval scores
        Recall@5 of 0.817 against BM25's 0.560 — so weighting them equally pulls
        the better ranking down. Measured, not assumed; see ADR-012.
        """
        fused: dict[str, float] = {}
        seen: dict[str, Chunk] = {}

        for ranking, weight in (
            (dense, self._dense_weight),
            (sparse, self._sparse_weight),
        ):
            for position, scored in enumerate(ranking, start=1):
                chunk_id = scored.chunk.id
                fused[chunk_id] = fused.get(chunk_id, 0.0) + weight / (self._rrf_k + position)
                seen.setdefault(chunk_id, scored.chunk)

        ordered = sorted(fused.items(), key=lambda pair: (-pair[1], seen[pair[0]].id))
        return [
            ScoredChunk(chunk=seen[chunk_id], score=score, source="hybrid")
            for chunk_id, score in ordered
        ]


def _passes(
    chunk: Chunk,
    exclude_tests: bool,
    kinds: Sequence[ChunkKind] | None,
    language: str | None,
) -> bool:
    if exclude_tests and chunk.is_test:
        return False
    if kinds and chunk.kind not in kinds:
        return False
    return not (language and chunk.language != language)
