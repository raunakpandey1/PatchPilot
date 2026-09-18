"""Vector store tests — persistence, filtering, idempotency."""

from __future__ import annotations

import pytest

from patchpilot.rag.chunking import Chunk, ChunkKind
from patchpilot.rag.embeddings import HashingEmbeddings, cosine_similarity
from patchpilot.rag.store import VectorStore


def make_chunk(index: int, *, repository="acme/widget", is_test=False, text=None) -> Chunk:
    return Chunk(
        id=f"{index:032x}",
        text=text or f"def function_{index}(): return {index}",
        repository=repository,
        file_path=f"src/mod_{index}.py",
        start_line=1,
        end_line=5,
        kind=ChunkKind.FUNCTION,
        symbol=f"function_{index}",
        language="python",
        is_test=is_test,
    )


@pytest.fixture
def embeddings():
    return HashingEmbeddings(dimension=128)


@pytest.fixture
def store(embeddings):
    return VectorStore(dimension=embeddings.dimension)


def store_chunks(store, embeddings, chunks):
    return store.upsert(chunks, embeddings.embed_documents([c.embedding_text() for c in chunks]))


# --- basics -----------------------------------------------------------------


def test_upsert_and_count(store, embeddings):
    assert store_chunks(store, embeddings, [make_chunk(i) for i in range(5)]) == 5
    assert store.count() == 5


def test_search_returns_scored_chunks(store, embeddings):
    store_chunks(store, embeddings, [make_chunk(i) for i in range(5)])

    results = store.search(embeddings.embed_query("function_3"), limit=3)

    assert len(results) == 3
    assert all(r.source == "dense" for r in results)
    assert results == sorted(results, key=lambda r: -r.score)


def test_mismatched_vectors_are_rejected(store, embeddings):
    with pytest.raises(ValueError, match="chunks but"):
        store.upsert([make_chunk(1), make_chunk(2)], [[0.0] * 128])


# --- idempotency ------------------------------------------------------------


def test_reindexing_the_same_chunks_does_not_duplicate(store, embeddings):
    """Chunk ids come from location plus content, so re-indexing overwrites.
    That makes a crashed indexing run safe to simply re-run."""
    chunks = [make_chunk(i) for i in range(5)]

    store_chunks(store, embeddings, chunks)
    store_chunks(store, embeddings, chunks)

    assert store.count() == 5


# --- filtering --------------------------------------------------------------


def test_filters_are_applied_during_search_not_after(store, embeddings):
    """The property that decides whether a vector DB is worth having.

    Thirty test chunks match the query well; four source chunks match poorly.
    Asking for four results excluding tests must return four source chunks.
    Post-filtering would take the top four overall and then discard them,
    returning nothing — recall silently lost.
    """
    matching_tests = [
        make_chunk(i, is_test=True, text="rows_where missing table raises error")
        for i in range(30)
    ]
    unrelated_source = [
        make_chunk(100 + i, is_test=False, text="jinja template rendering helper")
        for i in range(4)
    ]
    store_chunks(store, embeddings, matching_tests + unrelated_source)

    query = embeddings.embed_query("rows_where missing table raises error")
    unfiltered = store.search(query, limit=4)
    filtered = store.search(query, limit=4, exclude_tests=True)

    assert all(r.chunk.is_test for r in unfiltered), "tests should dominate without a filter"
    assert len(filtered) == 4, "pre-filtering must still return the full limit"
    assert all(not r.chunk.is_test for r in filtered)


def test_repository_filter(store, embeddings):
    store_chunks(store, embeddings, [
        make_chunk(1, repository="a/one"),
        make_chunk(2, repository="b/two"),
    ])

    results = store.search(embeddings.embed_query("function"), limit=10, repository="a/one")

    assert all(r.chunk.repository == "a/one" for r in results)


def test_delete_repository_leaves_others_alone(store, embeddings):
    store_chunks(store, embeddings, [
        make_chunk(1, repository="a/one"),
        make_chunk(2, repository="b/two"),
    ])

    store.delete_repository("a/one")

    assert store.count(repository="a/one") == 0
    assert store.count(repository="b/two") == 1


# --- persistence ------------------------------------------------------------


def test_index_survives_reopening(tmp_path, embeddings):
    """Indexing is expensive; it must not have to be repeated per process."""
    path = tmp_path / "qdrant"

    first = VectorStore(dimension=embeddings.dimension, path=path)
    store_chunks(first, embeddings, [make_chunk(i) for i in range(4)])
    first.close()

    second = VectorStore(dimension=embeddings.dimension, path=path)
    try:
        assert second.count() == 4
        assert second.search(embeddings.embed_query("function_2"), limit=2)
    finally:
        second.close()


def test_all_chunks_returns_the_corpus(store, embeddings):
    """BM25 is computed in-process over the corpus, so it needs this."""
    store_chunks(store, embeddings, [make_chunk(i) for i in range(12)])

    assert len(store.all_chunks("acme/widget")) == 12


# --- the arithmetic underneath ----------------------------------------------


def test_cosine_similarity_behaves_as_expected():
    """Written out rather than imported, so the maths behind 'vector search' is
    visible in the repository."""
    assert cosine_similarity([1, 0, 0], [1, 0, 0]) == pytest.approx(1.0)
    assert cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)
    assert cosine_similarity([1, 0, 0], [-1, 0, 0]) == pytest.approx(-1.0)
    assert cosine_similarity([0, 0, 0], [1, 0, 0]) == 0.0


def test_similar_texts_embed_closer_than_unrelated_ones(embeddings):
    """If this failed, the test embeddings would be noise and every retrieval
    test would be meaningless."""
    query = embeddings.embed_query("rows_where missing table")
    related = embeddings.embed_documents(["def rows_where(self): raise MissingTable"])[0]
    unrelated = embeddings.embed_documents(["def render(template): return html"])[0]

    assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)
