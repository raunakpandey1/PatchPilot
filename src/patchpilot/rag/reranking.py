"""Reranking: a second, more expensive look at the shortlist.

Bi-encoder vs cross-encoder — the whole idea
---------------------------------------------
The embedding model used for search is a **bi-encoder**. It encodes the query
and each document *separately*, into vectors, and compares them::

    vector(query)  ·  vector(document)   →  similarity

That separation is what makes search possible at all: every document can be
encoded once, in advance, and a query is then one encode plus a fast index
lookup. The cost is that the model never sees the query and the document
together, so it cannot notice that the query's word "not" inverts the meaning
of the passage.

A **cross-encoder** takes the pair as one input::

    model(query, document)   →  relevance score

Every layer of the network can relate a query token to a document token. It is
substantially more accurate — and it cannot be precomputed, because the score
does not exist until the query arrives. Scoring a whole corpus per query is out
of the question.

So they are used together, and the shape of the pipeline follows directly:

    thousands of chunks
        → bi-encoder + BM25 retrieve ~30    (fast, approximate)
        → cross-encoder rescores those 30   (slow, accurate)
        → top 5 go in the prompt

This is the standard retrieve-then-rerank arrangement, and the reason it exists
is precisely the bi-encoder's precompute/accuracy trade.

Is it worth it here?
--------------------
Unknown until measured, which is the point of Phase 3's evaluation. Reranking
adds latency and 80 MB of model. If Recall@5 does not move, it should be turned
off — and the benchmark can answer that, because retrieval mode is a parameter.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from patchpilot.logging import get_logger
from patchpilot.rag.store import ScoredChunk

log = get_logger("rag.reranking")

DEFAULT_RERANK_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"


@runtime_checkable
class Reranker(Protocol):
    @property
    def name(self) -> str: ...

    def rerank(
        self, query: str, candidates: Sequence[ScoredChunk], *, limit: int
    ) -> list[ScoredChunk]: ...


class NoOpReranker:
    """Passes the shortlist through unchanged.

    Not a placeholder — it is the control condition. The benchmark compares
    against this to answer whether reranking earns its latency, rather than
    assuming it does.
    """

    @property
    def name(self) -> str:
        return "none"

    def rerank(
        self, query: str, candidates: Sequence[ScoredChunk], *, limit: int
    ) -> list[ScoredChunk]:
        return list(candidates[:limit])


class CrossEncoderReranker:
    """A local cross-encoder (~80 MB, CPU).

    Loaded lazily, for the same reason as the embedding model: importing this
    module must not pull a model off disk.
    """

    def __init__(self, model_name: str = DEFAULT_RERANK_MODEL) -> None:
        self._model_name = model_name
        self._model: object | None = None

    @property
    def name(self) -> str:
        return self._model_name

    def _load(self) -> object:
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            log.info("reranker_loading", model=self._model_name)
            self._model = TextCrossEncoder(model_name=self._model_name)
        return self._model

    def rerank(
        self, query: str, candidates: Sequence[ScoredChunk], *, limit: int
    ) -> list[ScoredChunk]:
        if not candidates:
            return []

        model = self._load()
        started = time.monotonic()
        documents = [c.chunk.embedding_text() for c in candidates]
        scores = list(model.rerank(query, documents))  # type: ignore[attr-defined]

        rescored = [
            ScoredChunk(chunk=candidate.chunk, score=float(score), source="rerank")
            for candidate, score in zip(candidates, scores, strict=True)
        ]
        rescored.sort(key=lambda s: -s.score)

        log.debug(
            "reranked",
            candidates=len(candidates),
            returned=min(limit, len(rescored)),
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )
        return rescored[:limit]


class KeywordOverlapReranker:
    """A cheap, dependency-free reranker used in tests.

    Scores by how many of the query's distinctive terms appear in the chunk. Far
    weaker than a cross-encoder, but it is *real* reordering logic rather than a
    stub, so tests exercise the reranking path without loading a model.
    """

    @property
    def name(self) -> str:
        return "keyword-overlap"

    def rerank(
        self, query: str, candidates: Sequence[ScoredChunk], *, limit: int
    ) -> list[ScoredChunk]:
        from patchpilot.rag.retrieval import tokenize_for_bm25

        query_terms = set(tokenize_for_bm25(query))
        if not query_terms:
            return list(candidates[:limit])

        rescored: list[ScoredChunk] = []
        for candidate in candidates:
            terms = set(tokenize_for_bm25(candidate.chunk.embedding_text()))
            overlap = len(query_terms & terms) / len(query_terms)
            rescored.append(
                ScoredChunk(chunk=candidate.chunk, score=overlap, source="rerank")
            )

        rescored.sort(key=lambda s: (-s.score, s.chunk.id))
        return rescored[:limit]
