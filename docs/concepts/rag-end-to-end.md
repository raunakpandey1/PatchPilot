# RAG, end to end

## In one sentence

Retrieval-Augmented Generation means finding the few pieces of your data that
matter and putting them in the prompt, because the model has never seen your
code and cannot be told all of it.

## The problem it solves

You want to ask: *"Why does `rows_where()` return nothing for a missing
table?"*

Two things stand in the way.

**The model has never seen this repository.** Anything it says about
`sqlite-utils` is either recalled from public training data — possibly a version
from years ago — or invented. It cannot know about the code as it exists today.

**You cannot show it everything.** The repository is ~100 files. Code tokenises
densely: a 500-line Python file can exceed 6,000 tokens. Even where the whole
thing would technically fit in a large context window, filling a prompt with
thousands of irrelevant lines makes answers *worse*, not better — the relevant
part is buried.

So: find the right few hundred lines, and put those in the prompt. That is the
whole idea.

## How it works, step by step

```
 repository
     │
     │  1. SELECT        which files are worth indexing at all
     ▼
 files
     │
     │  2. CHUNK         split on function and class boundaries
     ▼
 chunks  ──────────────┐
     │                 │
     │  3. EMBED       │  4. INDEX FOR KEYWORDS
     ▼                 ▼
 vectors (Qdrant)   BM25 index
     │                 │
     └────────┬────────┘
              │  5. RETRIEVE     both, for the same query
              ▼
        two rankings
              │  6. FUSE         reciprocal rank fusion
              ▼
        ~30 candidates
              │  7. RERANK       cross-encoder rescores them
              ▼
          top 5 chunks
              │  8. ASSEMBLE     into the prompt, with citations
              ▼
             LLM
```

**1. Select.** Not every file earns a place. `poetry.lock` is thousands of lines
of hashes that match nothing meaningful; `node_modules/` is someone else's code;
a minified bundle is one line of noise. Every irrelevant chunk is a chance to
return the wrong thing. See
[`ingestion.py`](../../src/patchpilot/rag/ingestion.py).

**2. Chunk.** Cut on structure, not character counts — a function split in half
gives two useless pieces. See [chunking code](chunking-code.md).

**3. Embed.** Each chunk becomes 384 numbers positioned so that similar meanings
land near each other. See [embeddings and vectors](embeddings-and-vectors.md).

**4. Index for keywords.** BM25, in parallel, because exact identifiers are
where vector search is weakest. See [BM25 and hybrid search](bm25-and-hybrid-search.md).

**5–6. Retrieve and fuse.** Both methods produce a ranking; the two are on
incomparable scales, so they are merged by *position* rather than score.

**7. Rerank.** A slower, more accurate model rescores the shortlist. See
[reranking](reranking.md).

**8. Assemble.** The surviving chunks go in the prompt, each labelled with its
file and line range so the model can cite where a claim came from — and so a
human can check it.

## Where RAG actually fails

Worth knowing in this order, because this is the order in which they bite.

**Retrieval misses the relevant code.** Nothing downstream can recover. The
model is now reasoning about the wrong file, confidently. This is why
**Recall** is the headline metric — a file never retrieved can never be fixed.

**Retrieval finds it but ranks it 40th.** Only the top few chunks fit in the
prompt, so a correct result in position 40 is the same as no result. This is why
MRR is measured alongside recall.

**Chunks are cut badly.** A retrieved function with no body, or a body with no
name, wastes a slot and misleads.

**Too much context.** Twenty chunks "just in case" dilutes the signal and costs
tokens. More context is not better context.

**No citations.** If the model cannot say which file a claim came from, nobody
can check it — and an unverifiable root-cause analysis is not usable.

## In PatchPilot

| Stage | File |
|---|---|
| select + index | [`rag/ingestion.py`](../../src/patchpilot/rag/ingestion.py) |
| chunk | [`rag/chunking.py`](../../src/patchpilot/rag/chunking.py) |
| embed | [`rag/embeddings.py`](../../src/patchpilot/rag/embeddings.py) |
| store + filter | [`rag/store.py`](../../src/patchpilot/rag/store.py) |
| retrieve + fuse | [`rag/retrieval.py`](../../src/patchpilot/rag/retrieval.py) |
| rerank | [`rag/reranking.py`](../../src/patchpilot/rag/reranking.py) |
| measure | [`evaluation/retrieval.py`](../../src/patchpilot/evaluation/retrieval.py) |

Two decisions worth noting because they are unusual:

**Everything runs locally and free.** A 67 MB embedding model and an 80 MB
reranker, on the CPU. Indexing a repository means embedding thousands of chunks
— on a hosted API that is thousands of billed calls.

**It is measured against ground truth nobody had to label.** For a closed issue,
the commit that closed it names the files that actually had to change. That is
the correct answer, recorded by git as a side effect of normal development. See
[retrieval evaluation](retrieval-evaluation.md).

## Interview questions

**Q: What is RAG and why do you need it?**

Retrieval-augmented generation: find the parts of your data relevant to a query
and put them in the prompt. Needed because the model has never seen this
repository and cannot be shown all of it — code tokenises densely, and filling a
prompt with irrelevant files makes answers worse rather than better. So the
engineering problem is choosing what goes in.

**Q: Where does a RAG system most often fail?**

Retrieval, not generation. If the relevant code is not in the top few results,
nothing downstream can recover — the model reasons confidently about the wrong
file. That is why the headline metric here is Recall, and why the retrieval
layer is measured independently of whether the final patch worked.

**Q: Why not just use a long context window and paste the whole repository?**

Three reasons. Cost — you pay per token on every call, and on a free tier that
is a quota. Quality — a needle in a very large haystack is harder for the model,
and irrelevant context measurably degrades answers. And it does not scale:
"paste everything" works for a small repository and fails on a large one,
whereas retrieval is the same shape either way.

**Q: How do you know your retrieval is good?**

Measure it against ground truth. Here that comes free from git: the commit that
closed an issue names the files that had to change, so each closed issue is a
labelled example with no human annotation. I report Recall@K, Precision@K and
MRR across retrieval strategies, and the numbers are a lower bound because a
retrieved file that was not in the commit is scored wrong even if it was
genuinely relevant.

## Official documentation

- [Qdrant — What is a vector database?](https://qdrant.tech/documentation/overview/) — the storage side from first principles.
- [Qdrant — Hybrid queries](https://qdrant.tech/documentation/concepts/hybrid-queries/) — fusing dense and sparse results.
- [Gemini API — Embeddings](https://ai.google.dev/gemini-api/docs/embeddings) — the hosted alternative to the local model used here.
