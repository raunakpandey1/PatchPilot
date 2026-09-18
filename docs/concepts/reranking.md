# Reranking: paying for accuracy only where it counts

## In one sentence

Retrieval is fast and approximate, so a second, slower model rescores the top
~30 results before the best 5 go in the prompt.

## The problem it solves

Search has to be fast, and fast means approximate.

The embedding model used for search is a **bi-encoder**: it encodes the query
and each document *separately*, then compares the two vectors.

```
vector(query)   ·   vector(document)   →  similarity
```

That separation is what makes search possible at all. Every chunk is encoded
once, in advance; a query needs one encode plus an index lookup. Thousands of
chunks, milliseconds.

The cost is that the model **never sees the query and the document together**.
It compressed the document into 384 numbers before it knew what would be asked.
It cannot notice that the query says "without raising an error" and the passage
raises an error.

## How it works, step by step

A **cross-encoder** takes the pair as a single input:

```
model(query, document)   →  relevance score
```

Every layer can relate a query token to a document token. Substantially more
accurate — and impossible to precompute, because the score does not exist until
the query arrives. Scoring a whole corpus per query is out of the question.

So the two are used in sequence, and the shape of the pipeline follows directly
from that trade:

```
 thousands of chunks
        │
        │  bi-encoder + BM25          fast, approximate
        ▼
    ~30 candidates
        │
        │  cross-encoder              slow, accurate
        ▼
     top 5 chunks  →  the prompt
```

Each stage does what it is good at. The first is cheap enough to run over
everything; the second is accurate enough to be worth its cost on thirty.

### Why 30 candidates

Big enough that the right answer is probably somewhere in it; small enough that
scoring it is affordable. **Reranking cannot recover what retrieval missed** —
if the correct chunk is 40th and you rerank 30, no amount of accuracy helps. The
pool size is the ceiling on what reranking can fix.

### Why the top 5 and not the top 20

Context is not free. Every chunk costs tokens, and irrelevant chunks measurably
degrade answers by burying the relevant one. More context is not better context.

## Is it worth it?

**Unknown until measured** — which is the point of Phase 3's benchmark.

Reranking adds latency and 80 MB of model. If Recall@5 does not move, it should
be switched off. So the benchmark compares four configurations — dense, sparse,
hybrid, hybrid+rerank — and `NoOpReranker` exists precisely as the control
condition, not as a placeholder.

This is a habit worth having: when you add a stage that costs something,
build the experiment that can tell you to remove it.

## In PatchPilot

[`rag/reranking.py`](../../src/patchpilot/rag/reranking.py):

- `CrossEncoderReranker` — `ms-marco-MiniLM-L-6-v2`, ~80 MB, CPU, loaded lazily.
- `NoOpReranker` — the control.
- `KeywordOverlapReranker` — a cheap, dependency-free reranker for tests. Far
  weaker, but it is *real* reordering logic rather than a stub, so the reranking
  path gets exercised without loading a model.

## What goes wrong

**Reranking too few candidates.** The pool is the ceiling. Rerank 10 and you can
only ever reorder those 10.

**Reranking too many.** It is the expensive stage; the cost is linear in pool
size, and past a point you are paying to rescore things retrieval already ruled
out for good reason.

**Assuming it helps.** It usually does. It is still a measurement.

**Forgetting it runs per query.** Indexing is a one-off; reranking is on every
request, so its latency lands in the user-facing path.

## Interview questions

**Q: What is reranking and why is it a separate stage?**

Retrieval uses a bi-encoder, which encodes query and document separately so
documents can be embedded in advance — that is what makes search fast, and it
means the model never sees the pair together. A cross-encoder scores the pair
jointly and is much more accurate, but cannot be precomputed. So you retrieve a
shortlist cheaply and rescore it expensively: thousands of chunks narrow to
about thirty, then to the five that go in the prompt.

**Q: What is the difference between a bi-encoder and a cross-encoder?**

A bi-encoder produces one vector per text independently, so similarity is a dot
product and documents can be indexed ahead of time. A cross-encoder takes query
and document as a single input and outputs a relevance score, so every layer can
relate the two — better, but it must run per pair at query time. Precompute
versus accuracy is the whole trade.

**Q: How many candidates do you rerank, and why does it matter?**

About thirty. It matters because reranking cannot recover what retrieval missed
— if the right chunk was ranked 40th, reranking 30 will never see it. The pool
size is the ceiling on what this stage can fix, and the cost is linear in it.

**Q: How do you know reranking is worth the latency?**

I measure it. The benchmark runs dense, sparse, hybrid and hybrid+rerank over
the same labelled examples, and there is a no-op reranker specifically as the
control condition. If Recall@5 does not move, the stage does not earn its
latency and comes out.

## Official documentation

- [Sentence-Transformers — Cross-Encoders](https://www.sbert.net/examples/applications/cross-encoder/README.html) — the clearest explanation of the bi-encoder/cross-encoder distinction.
- [fastembed — reranking](https://qdrant.github.io/fastembed/examples/Reranking_with_FastEmbed/) — the local implementation used here.
