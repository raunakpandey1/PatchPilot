# Phase 3 — Repository RAG

**Goal:** given an issue, find the code that matters — and *measure* whether it
worked, rather than reading the results and deciding they look good.
**Date:** 2026-09-18

## 1. What we built

```
 repository ──select──► files ──chunk──► chunks
                                            │
                              ┌─────────────┴─────────────┐
                              │                           │
                         embed (BGE-small)           BM25 index
                              ▼                           ▼
                        Qdrant (embedded)          in-process corpus
                              │                           │
                              └──────────┬────────────────┘
                                         │ retrieve both, fuse by rank (RRF)
                                         ▼
                                  ~30 candidates
                                         │ cross-encoder rerank
                                         ▼
                                    top 5 chunks
```

| Module | Responsibility |
|---|---|
| [`rag/chunking.py`](../../src/patchpilot/rag/chunking.py) | AST-based splitting on declaration boundaries |
| [`rag/embeddings.py`](../../src/patchpilot/rag/embeddings.py) | local BGE-small; a hashing vectoriser for tests |
| [`rag/store.py`](../../src/patchpilot/rag/store.py) | Qdrant embedded, with pre-filtered search |
| [`rag/retrieval.py`](../../src/patchpilot/rag/retrieval.py) | dense + BM25, fused with RRF |
| [`rag/reranking.py`](../../src/patchpilot/rag/reranking.py) | cross-encoder, plus a no-op control |
| [`rag/ingestion.py`](../../src/patchpilot/rag/ingestion.py) | what gets indexed, and what deliberately does not |
| [`evaluation/retrieval.py`](../../src/patchpilot/evaluation/retrieval.py) | ground truth from git, and the metrics |

Everything runs **locally and free**: a 67 MB embedding model and an 80 MB
reranker on the CPU. Indexing a repository means embedding thousands of chunks —
on a hosted API that is thousands of billed calls.

## 2. How it works

**Chunks cut on structure, not character counts.** A function split in half
produces two useless pieces: one with a name and no logic that matches searches
it cannot answer, and one with logic and no name that is unfindable. Python
ships a parser, so the boundaries are read rather than guessed.
[ADR-013](../adr/ADR-013-code-aware-chunking.md).

**Every chunk carries a context header** —
`# sqlite_utils/db.py · class Table · method rows_where`. A bare
`def get(self, key)` is ambiguous; the header restores what a human gets from
opening the file, and it is embedded with the body, so it improves the match
too.

**Retrieval is hybrid because the two methods fail in opposite directions.**
Vector search handles paraphrase — "crashes when the table doesn't exist" finds
`raise NoTable` — and is weak on exact identifiers. BM25 is the reverse. A
GitHub issue contains prose *and* a traceback with exact symbols.
[ADR-012](../adr/ADR-012-hybrid-retrieval.md).

**The two rankings are fused by position, not score.** Cosine similarity is in
[-1, 1]; BM25 is unbounded. Reciprocal Rank Fusion discards the scores and sums
`1/(60 + rank)`, so nothing needs normalising and a ranker with wild scores
cannot dominate.

**Filtering happens before the search, not after.** Post-filtering — retrieve 50,
discard the tests — silently returns fewer results than asked for and quietly
destroys recall. Verified rather than assumed:
`test_filters_are_applied_during_search_not_after`.
[ADR-011](../adr/ADR-011-qdrant.md).

## 3. Why this way

The measurement is the point of the phase. Any of these components can be made
to *look* good by running a query and reading the output. The only way to know
whether hybrid beats dense, or whether reranking earns its latency, is a
benchmark with ground truth — so retrieval mode and reranker are parameters
specifically so they can be compared.

The ground truth costs nothing: for a closed issue, the commit that closed it
names the files that had to change. Git recorded the answer as a side effect of
normal development, which makes the labels more honest than a hand-built set —
they record what really had to change, not what someone thought ought to be
relevant.

## 4. Alternatives

| Decision | Alternatives | ADR |
|---|---|---|
| Qdrant embedded | list of vectors, FAISS, Chroma, pgvector, hosted | [011](../adr/ADR-011-qdrant.md) |
| Hybrid + RRF | dense only, BM25 only, score normalisation | [012](../adr/ADR-012-hybrid-retrieval.md) |
| AST chunking | fixed-size, line windows, whole files, tree-sitter | [013](../adr/ADR-013-code-aware-chunking.md) |
| Split stores | everything in Qdrant, everything in pgvector | [014](../adr/ADR-014-vector-db-is-not-the-database.md) |

## 5. Tradeoffs

- **BM25 is in-process.** The corpus is held in memory and the index rebuilt per
  repository — milliseconds for one repository, untenable for many large ones.
  The fix is Qdrant's sparse vectors. Deferred deliberately, written down.
- **Payload indexes do nothing in embedded Qdrant.** Correctness (pre-filtering)
  holds; the acceleration does not. A scale concern, not a correctness one, and
  index creation is skipped rather than left in to imply a guarantee we lack.
- **Python only, properly.** Everything else falls back to headings or blank
  lines. tree-sitter is deferred, not rejected.
- **Approximate search.** HNSW can miss a true nearest neighbour. Captured in
  the recall measurement rather than assumed away.
- **The unit suite got slower** — roughly 7 s to ~37 s — because these tests
  build real Qdrant instances and embed real text. Still offline, still free,
  and the alternative is mocks that would not catch a broken pipeline.

## 6. Problems encountered

**F-005 — the benchmark could not have scored 1.0, by construction.** The
serious one. Full write-up in [failures.md](../failures.md).

Two smaller ones fixed in passing:

- **camelCase tokenisation was silently broken.** Lowercasing before splitting
  destroys the boundary, so `getUserName` became one opaque token. Caught by
  printing the tokeniser's output on four examples.
- **`mypy` flagged a `None` filter** in `delete_repository` — a real latent bug:
  had the repository name ever been empty, it would have deleted the entire
  collection rather than one repository's chunks. Now it raises.

## 7. Debugging process — F-005

**Symptom.** None. Nothing failed. The benchmark ran for 24 minutes and would
have printed plausible numbers.

I found it by printing the ground truth before trusting it:

```
avg relevant files per example: 1.96
  #50: "Too many SQL variables" on large inserts
      -> sqlite_utils/db.py, tests/test_create.py
```

**Investigation.** Two files per example, roughly half of them tests — because a
fix commit touches the source *and* its test. But the evaluator retrieves with
`exclude_tests=True`, correctly: the question is "where is the bug", and bugs
live in production code.

So the retriever was forbidden from ever returning `tests/test_create.py`.
Maximum achievable recall for that example: **0.5**. Across the set the ceiling
sat below 1.0 — at a *different* level per example, depending on how many of its
files happened to be tests.

**Root cause.** The labels and the system under test disagreed about what counts
as a valid answer. Neither was wrong alone.

**Fix.** Filter test files out of the labels. The *size* check still runs on the
full change, because a commit touching twelve files is a refactor whether or not
most of them were tests. The exclusion is a parameter, so the effect of the
choice can itself be measured.

**Verification.** Two regression tests, and 200 examples became 192 — the eight
dropped were issues whose fix commit touched only test files.

**Why this one matters.** A crash tells you something is wrong. **A biased
metric tells you 0.62** and lets you spend a week optimising against a ceiling
you did not know existed, with every comparison inheriting the bias silently.
The generalisable check: if the system is forbidden from producing something
your labels call correct, you are measuring the restriction, not the system.

## 8. Tests

| file | count | what it covers |
|---|---:|---|
| [`test_chunking.py`](../../tests/unit/test_chunking.py) | 16 | boundaries never split declarations; unparseable files still produce chunks; ids are stable |
| [`test_rag_store.py`](../../tests/unit/test_rag_store.py) | 11 | **pre- vs post-filtering, constructed to distinguish them**; persistence across processes; idempotent upsert |
| [`test_retrieval.py`](../../tests/unit/test_retrieval.py) | 19 | tokenisation, the three modes, RRF bounds, filter-before-rank |
| [`test_ingestion.py`](../../tests/unit/test_ingestion.py) | 17 | lock files and generated files excluded; tests flagged not dropped |
| [`test_eval_retrieval.py`](../../tests/unit/test_eval_retrieval.py) | 15 | ground truth construction, metric behaviour, **the F-005 regression** |

All offline. The test embedding model is a hashing vectoriser rather than a stub
returning zeros — texts sharing vocabulary genuinely come out close, so a broken
pipeline cannot pass.

## 9. Metrics

Full table in [metrics.md](../metrics.md). The headline, on 192 labelled
examples:

| configuration | Recall@5 | MRR | latency |
|---|---:|---:|---:|
| **dense** | **0.835** | **0.564** | 79 ms |
| hybrid 1:1 (the design) | 0.757 | 0.443 | 93 ms |
| dense + rerank | 0.775 | 0.399 | 3,621 ms |
| sparse (BM25) | 0.609 | 0.318 | 12 ms |

**The phase was designed around hybrid retrieval with reranking, and the
measurement rejected both.** Dense alone is the most accurate *and* the fastest.

A weight sweep explains it: plain RRF weights both rankings equally, which is
only right when they are comparable, and dense (0.835) is much stronger than
BM25 (0.609). Raising the dense weight climbs monotonically towards dense-only —
0.757 at 1:1, 0.799 at 3:1, 0.817 at 10:1, 0.835 at the limit — which is the
signature of one ranker adding nothing the other lacked, for this query shape.

Reranking failed for a different reason: `ms-marco-MiniLM` is trained on web
search passages, not code.

**With 192 examples the noise floor is about ±0.027**, so dense versus hybrid
10:1 is not a real difference and the write-up says so. Indexing cost 504 s for
1,873 chunks on CPU.

## 10. Official documentation

- [Qdrant — Hybrid queries](https://qdrant.tech/documentation/concepts/hybrid-queries/) — fusion as the database implements it, including RRF.
- [Qdrant — Filtering](https://qdrant.tech/documentation/concepts/filtering/) — payload conditions and why indexes matter on a server.
- [Python `ast`](https://docs.python.org/3/library/ast.html) — `parse`, `lineno`, `end_lineno`.
- [Sentence-Transformers — Cross-Encoders](https://www.sbert.net/examples/applications/cross-encoder/README.html) — the bi-encoder/cross-encoder distinction.

## 11. Interview questions

1. Why not fixed-size chunks for code?
2. What does a context header on a chunk buy you?
3. Why hybrid retrieval rather than vectors alone?
4. How do you combine two rankings whose scores are not comparable?
5. What is the difference between pre- and post-filtering, and why does it
   matter for the number you report?
6. What is the difference between a bi-encoder and a cross-encoder?
7. How do you evaluate retrieval without paying for labels?
8. What is wrong with your benchmark?
9. Your benchmark had a ceiling below 1.0. How did you find that?
10. What breaks first when you scale this to a thousand repositories?

## 12. STAR story

**S-007** — a benchmark that could never have scored 1.0. See the
[story bank](../interview/story-bank.md).
