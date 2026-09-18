# ADR-012 — Hybrid retrieval, and what measuring it actually showed

**Status:** Accepted, with a finding that contradicts the original design ·
**Phase:** 3 · **Date:** 2026-09-18

## Context

Retrieval has to find the code relevant to a GitHub issue. Two mechanisms are
available and they fail in opposite directions.

**Dense (vector) search** matches meaning. An issue saying *"crashes when the
table doesn't exist"* finds `if not self.exists(): raise NoTable` despite
sharing no words with it.

**BM25 (keyword) search** matches exact terms. A query for `rows_where` finds
the function literally called `rows_where` — which dense search often does not,
because an identifier is a token rather than a concept, and its embedding sits
near "things about querying rows" generally.

A real GitHub issue contains both: prose describing the symptom, and a traceback
with exact symbol names. The standard conclusion — and the one this phase was
designed around — is that combining them beats either alone.

## Options considered

1. **Dense only.** One index, one query path.
2. **BM25 only.** No embedding model, no vector store.
3. **Hybrid, fused by score.** Normalise the two score scales and add them.
4. **Hybrid, fused by rank (RRF).** Discard the scores; combine positions.
5. **Hybrid + cross-encoder reranking.** Fuse, then rescore the shortlist with a
   model that sees query and document together.

## The decision, and the part that did not survive contact with measurement

Fusion by **rank**, not score, was correct and remains so: cosine similarity is
bounded in [-1, 1] while BM25 is unbounded and corpus-dependent, so adding them
is meaningless and normalising them requires per-query distributions that are
not available. Reciprocal Rank Fusion sidesteps this by using only positions:

```
score(chunk) = Σ  weight / (k + rank)          k = 60
```

What did **not** survive was the assumption that hybrid beats dense here, and
that reranking is worth its cost. The benchmark says otherwise, and the numbers
are below.

## What the benchmark showed

192 labelled examples from `simonw/sqlite-utils`, ground truth from fix commits,
a fixed pool of 30 chunks deduplicated to files, scoring at file granularity.

| configuration | Recall@5 | MRR | latency |
|---|---:|---:|---:|
| **dense only** | **0.835** | **0.564** | 79 ms |
| hybrid 10:1 dense | 0.817 | 0.540 | 93 ms |
| hybrid 3:1 dense | 0.799 | 0.503 | 94 ms |
| dense + rerank | 0.775 | 0.399 | 3,621 ms |
| hybrid 1:1 (plain RRF) | 0.757 | 0.443 | 93 ms |
| hybrid 1:1 + rerank | 0.736 | 0.373 | 4,248 ms |
| sparse (BM25) only | 0.609 | 0.318 | 12 ms |

**Two designed-in assumptions were wrong.**

### 1. Hybrid lost to dense, and the weight sweep says why

Plain RRF weights both rankings equally. That is only correct when they are of
comparable quality, and here they are not — dense scores 0.835, BM25 scores
0.609. Fusing a strong ranking with a much weaker one at equal weight pulls the
strong one down.

The sweep makes this unambiguous, because the relationship is monotonic:

```
hybrid 1:1    0.757
hybrid 3:1    0.799
hybrid 10:1   0.817
dense only    0.835     ← the limit the weighting is approaching
```

Increasing the dense weight does not find a better blend; it converges on dense.
BM25's contribution is not adding information that dense search lacks — at least
not on this corpus, with these queries.

**Why, most likely.** The queries are whole GitHub issue texts — several hundred
words of prose. BM25 scores term overlap, and a long query dilutes across many
common terms, so its precision advantage on exact identifiers is swamped. BM25
shines on short, exact queries (`rows_where`), which is not what an issue is.
That is a hypothesis consistent with the numbers, not something the benchmark
proves.

### 2. Reranking made things worse, at 46× the latency

Recall 0.835 → 0.775 and MRR 0.564 → 0.399, for 3.6 seconds per query instead of
79 ms.

The likely cause is domain mismatch: `ms-marco-MiniLM` is trained on MS MARCO —
web search passages in natural language. Source code is not that. The
cross-encoder is confidently reordering into an order that is worse for this
domain, which is a reminder that "more accurate model" is a claim about a
distribution, not a property of a component.

### What is and is not significant

With 192 examples, the standard error on a recall around 0.8 is roughly 0.027.
So:

- dense (0.835) vs hybrid 10:1 (0.817) — **within noise.** Not a real difference.
- dense (0.835) vs hybrid 1:1 (0.757) — about 2.9 standard errors. Real.
- dense (0.835) vs rerank (0.775) — about 2.2 standard errors. Probably real,
  and the MRR collapse (0.564 → 0.399) is far outside noise.

At k=10 hybrid 3:1 edges dense on recall (0.910 vs 0.902) — comfortably within
noise — while losing on MRR (0.518 vs 0.571). Nothing there changes the decision.

**Precision is not informative here.** With 1.14 relevant files per example and
5 results returned, the maximum achievable precision@5 is 0.228. Dense scores
0.187 at k=5 in this run — the metric is dominated by its ceiling, so recall and
MRR carry the signal.

## Decision

- **Default retrieval mode: `dense`.** Set in
  [`AgentDeps`](../../src/patchpilot/agent/deps.py).
- **Reranking off by default.** The `CrossEncoderReranker` stays in the
  codebase, because "we measured it and it hurt" is a result worth keeping
  reproducible.
- **Hybrid and weighted fusion stay implemented**, as parameters. The finding is
  about *this corpus with these queries*, and the next repository may differ.

## Tradeoffs

**Against the decision:** dense-only inherits the embedding model's known
weakness on exact identifiers. If a later phase queries with a symbol name
rather than an issue body — which Phase 5 plausibly will, looking up a specific
function — BM25 should be re-measured for that query shape. The code supports it;
only the default changes.

**For:** the fastest configuration is also the most accurate one here, which is
a rare and welcome alignment. 79 ms versus 3.6 seconds also matters for a free
tier, because latency is CPU time on this machine.

## Consequences

- The Phase 4 investigation node retrieves with `mode="dense"`.
- ADR-011's reasoning for Qdrant is unaffected — filtering and persistence were
  the reasons, not fusion.
- Phase 11 should re-run this comparison if the query shape changes, and on a
  second repository before any of it is treated as general.

## Interview questions

**Q: Why hybrid retrieval?**

That was the design, and the measurement rejected it. The reasoning was sound —
dense search handles paraphrase and is weak on exact identifiers, BM25 is the
reverse, and issues contain both. But on 192 labelled examples dense alone
scored Recall@5 of 0.835 against hybrid's 0.757, so I default to dense and keep
hybrid as a parameter.

**Q: How do you know the fusion itself was not the problem?**

I swept the weighting. Plain RRF weights both rankings equally, which is only
right if they are comparable, and mine were not — 0.835 against 0.609. As I
increased the dense weight the result climbed monotonically towards dense-only:
0.757 at 1:1, 0.799 at 3:1, 0.817 at 10:1, 0.835 at the limit. It converges on
dense rather than finding a better blend, which says BM25 is not contributing
information dense search lacks — for this query shape.

**Q: Why do you think BM25 underperformed?**

The queries are whole issue texts, several hundred words. BM25 scores term
overlap, so a long query dilutes across many common terms and its advantage on
exact identifiers gets swamped. It is strongest on short exact queries, which an
issue body is not. That is a hypothesis consistent with the numbers, not
something I proved — and I would test it by querying with extracted symbol names
instead.

**Q: Reranking is supposed to help. Why did it not?**

Domain mismatch, most likely. `ms-marco-MiniLM` is trained on web search
passages in natural language, and source code is not that distribution. It
reordered confidently into a worse order — recall 0.835 to 0.775, MRR 0.564 to
0.399, at 46× the latency. The general lesson is that "more accurate model" is a
claim about a distribution, not a property of a component, and a code-trained
reranker would be the thing to try.

**Q: Your best result and second-best differ by 0.018. Is that meaningful?**

No. With 192 examples the standard error around 0.8 is roughly 0.027, so dense
versus hybrid-10:1 is within noise and I say so. What is outside noise is dense
versus plain 1:1 fusion, and the MRR collapse under reranking. Quoting the first
comparison as a win would be over-reading my own data.

## Behavioural question this answers

> *"Tell me about a time the data contradicted your design."*

I built hybrid retrieval with reranking because that is the standard
architecture and the reasoning is genuinely good — vector search and keyword
search fail in opposite directions, and a bug report contains both prose and
exact symbol names. Then I measured it on 192 labelled examples and dense search
alone beat it, while reranking made things worse at 46 times the latency.

Rather than accept or dismiss that, I tested the most likely explanation:
reciprocal rank fusion weights both rankings equally, so fusing a strong ranker
with a weak one should drag the strong one down. I swept the weighting and the
result climbed monotonically towards dense-only — 0.757, 0.799, 0.817, 0.835 —
which is the signature of one ranker contributing nothing the other lacked.

So I changed the default to dense, kept hybrid as a parameter because the
finding is about this corpus and this query shape, and wrote down both the
result and its limits — including that my top two configurations differ by less
than one standard error and I am not entitled to call that a win.
