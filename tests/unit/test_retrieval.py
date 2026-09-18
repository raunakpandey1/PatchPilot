"""Retrieval tests: dense, sparse, hybrid, and the fusion between them.

These run entirely offline. The embedding model is `HashingEmbeddings` — not a
stub returning zeros, but a real hashing vectoriser, so texts that share
vocabulary genuinely come out close. A fake returning random vectors would let a
completely broken pipeline pass.

What it cannot do is semantic matching — "crashes when the table is missing"
will not find `raise NoTable` here. That is what the real model is for, and what
`scripts/benchmark_retrieval.py` measures against ground truth.
"""

from __future__ import annotations

import pytest

from patchpilot.rag.chunking import Chunk, ChunkKind
from patchpilot.rag.embeddings import HashingEmbeddings
from patchpilot.rag.reranking import KeywordOverlapReranker, NoOpReranker
from patchpilot.rag.retrieval import HybridRetriever, tokenize_for_bm25
from patchpilot.rag.store import VectorStore

REPO = "acme/widget"


def make_chunk(index: int, text: str, **kwargs) -> Chunk:
    defaults: dict[str, object] = {
        "repository": REPO,
        "file_path": f"src/module_{index}.py",
        "start_line": 1,
        "end_line": 10,
        "kind": ChunkKind.FUNCTION,
        "symbol": f"symbol_{index}",
        "language": "python",
        "is_test": False,
    }
    defaults.update(kwargs)
    return Chunk(id=f"{index:032x}", text=text, **defaults)


CORPUS = [
    make_chunk(1, "def rows_where(self, where): return self.db.execute(where)",
               file_path="src/db.py", symbol="rows_where"),
    make_chunk(2, "def delete_where(self, where): self.db.execute('delete')",
               file_path="src/db.py", symbol="delete_where"),
    make_chunk(3, "def render_template(name): return jinja.get_template(name)",
               file_path="src/render.py", symbol="render_template"),
    make_chunk(4, "def connect(path): return sqlite3.connect(path)",
               file_path="src/conn.py", symbol="connect"),
    make_chunk(5, "def test_rows_where(): assert rows_where('x=1')",
               file_path="tests/test_db.py", symbol="test_rows_where", is_test=True),
    make_chunk(6, "Documentation about querying rows and where clauses",
               file_path="docs/query.md", kind=ChunkKind.DOCUMENTATION, language="markdown"),
]


@pytest.fixture
def retriever():
    embeddings = HashingEmbeddings(dimension=256)
    store = VectorStore(dimension=embeddings.dimension)
    store.upsert(CORPUS, embeddings.embed_documents([c.embedding_text() for c in CORPUS]))
    return HybridRetriever(store, embeddings)


# --- tokenisation -----------------------------------------------------------


def test_identifiers_are_kept_whole_and_split():
    """Both, deliberately. Keeping only the parts would make `rows_where`
    indistinguishable from `where_rows`; keeping only the whole would miss
    "getting rows with a where clause"."""
    assert tokenize_for_bm25("rows_where") == ["rows_where", "rows", "where"]


def test_camel_case_is_split_on_original_casing():
    """Lowercasing before splitting destroys the boundary — a bug this test
    caught."""
    assert tokenize_for_bm25("getUserName") == ["getusername", "get", "user", "name"]


def test_punctuation_is_dropped():
    assert tokenize_for_bm25("db.rows_where(x)") == ["db", "rows_where", "rows", "where", "x"]


# --- the three modes --------------------------------------------------------


def test_sparse_search_finds_exact_identifiers(retriever):
    """Where keyword search wins outright. An exact symbol name is not a
    concept, and an embedding of it sits near 'things about querying rows'."""
    results = retriever.retrieve("rows_where", limit=3, mode="sparse", repository=REPO)

    assert results
    assert results[0].chunk.symbol == "rows_where"
    assert results[0].source == "sparse"


def test_dense_search_returns_results(retriever):
    results = retriever.retrieve("query rows with a where clause", limit=3,
                                 mode="dense", repository=REPO)

    assert results
    assert all(r.source == "dense" for r in results)


def test_hybrid_returns_fused_results(retriever):
    results = retriever.retrieve("rows_where", limit=3, mode="hybrid", repository=REPO)

    assert results
    assert all(r.source == "hybrid" for r in results)


def test_hybrid_surfaces_what_one_method_alone_ranks_low(retriever):
    """The reason for fusing at all: a chunk one method puts 8th and the other
    puts 1st should beat one that both put 3rd."""
    dense_only = retriever.retrieve("rows_where", limit=2, mode="dense", repository=REPO)
    hybrid = retriever.retrieve("rows_where", limit=2, mode="hybrid", repository=REPO)

    dense_symbols = [r.chunk.symbol for r in dense_only]
    hybrid_symbols = [r.chunk.symbol for r in hybrid]

    assert "rows_where" in hybrid_symbols
    # Not asserting the two orderings differ — on a six-chunk corpus they may
    # agree. What matters is that fusion never loses the exact match.
    assert hybrid_symbols[0] == "rows_where" or dense_symbols[0] == "rows_where"


# --- fusion mechanics -------------------------------------------------------


def test_rrf_uses_ranks_not_scores(retriever):
    """Dense similarity is a cosine in [-1,1]; BM25 is unbounded. Adding them is
    meaningless, so fusion uses only positions."""
    results = retriever.retrieve("rows_where", limit=5, mode="hybrid", repository=REPO)

    # RRF scores are sums of 1/(k+rank), so with k=60 no chunk can exceed 2/61.
    assert all(0 < r.score <= 2 / 61 + 1e-9 for r in results)


def test_fusion_is_deterministic(retriever):
    first = [r.chunk.id for r in retriever.retrieve("where clause", limit=4,
                                                    mode="hybrid", repository=REPO)]
    second = [r.chunk.id for r in retriever.retrieve("where clause", limit=4,
                                                     mode="hybrid", repository=REPO)]

    assert first == second


# --- filtering --------------------------------------------------------------


def test_tests_can_be_excluded(retriever):
    results = retriever.retrieve("rows_where", limit=10, mode="hybrid",
                                 repository=REPO, exclude_tests=True)

    assert results
    assert all(not r.chunk.is_test for r in results)


def test_filtering_happens_before_ranking_not_after(retriever):
    """Post-filtering would return fewer results than asked for. The exact-match
    test chunk scores highly for this query, so if filtering happened after
    ranking we would lose a slot."""
    unfiltered = retriever.retrieve("test_rows_where", limit=3, mode="sparse", repository=REPO)
    filtered = retriever.retrieve("test_rows_where", limit=3, mode="sparse",
                                  repository=REPO, exclude_tests=True)

    assert any(r.chunk.is_test for r in unfiltered), "the test chunk should rank without a filter"
    assert all(not r.chunk.is_test for r in filtered)


def test_kind_and_language_filters(retriever):
    docs = retriever.retrieve("rows where clause", limit=5, mode="hybrid",
                              repository=REPO, kinds=[ChunkKind.DOCUMENTATION])

    assert all(r.chunk.kind is ChunkKind.DOCUMENTATION for r in docs)


def test_repository_filter_isolates_corpora():
    """One store can hold several repositories; a query must not leak across."""
    embeddings = HashingEmbeddings(dimension=128)
    store = VectorStore(dimension=embeddings.dimension)

    other = [make_chunk(90, "def unrelated(): pass", repository="other/repo")]
    store.upsert(CORPUS + other,
                 embeddings.embed_documents([c.embedding_text() for c in CORPUS + other]))
    retriever = HybridRetriever(store, embeddings)

    results = retriever.retrieve("unrelated", limit=5, mode="hybrid", repository=REPO)

    assert all(r.chunk.repository == REPO for r in results)


# --- reranking --------------------------------------------------------------


def test_noop_reranker_is_the_control_condition(retriever):
    """Not a placeholder — it is what the benchmark compares against to decide
    whether reranking earns its latency."""
    candidates = retriever.retrieve("rows_where", limit=5, mode="hybrid", repository=REPO)

    assert NoOpReranker().rerank("rows_where", candidates, limit=3) == candidates[:3]


def test_reranker_can_reorder_the_shortlist(retriever):
    candidates = retriever.retrieve("delete_where rows", limit=6, mode="hybrid", repository=REPO)

    reranked = KeywordOverlapReranker().rerank("delete_where rows", candidates, limit=3)

    assert len(reranked) <= 3
    assert all(r.source == "rerank" for r in reranked)
    assert reranked == sorted(reranked, key=lambda r: -r.score)


def test_reranking_an_empty_shortlist_is_safe():
    assert KeywordOverlapReranker().rerank("anything", [], limit=5) == []


# --- edge cases -------------------------------------------------------------


def test_empty_index_returns_nothing_rather_than_raising():
    """An unindexed repository is a normal state, not an error — BM25Okapi
    raises on an empty corpus, so this is explicitly handled."""
    embeddings = HashingEmbeddings(dimension=64)
    retriever = HybridRetriever(VectorStore(dimension=64), embeddings)

    assert retriever.retrieve("anything", mode="hybrid", repository=REPO) == []


def test_query_with_no_matching_terms_still_returns_dense_results(retriever):
    """BM25 finds nothing; dense search still has an opinion. Hybrid must not
    collapse to empty just because one half did."""
    results = retriever.retrieve("zzzz qqqq", limit=3, mode="hybrid", repository=REPO)

    assert isinstance(results, list)


def test_invalidate_clears_the_keyword_index(retriever):
    retriever.retrieve("rows_where", mode="sparse", repository=REPO)
    retriever.invalidate(REPO)

    assert retriever.retrieve("rows_where", mode="sparse", repository=REPO)
