# BM25, and why search needs both kinds

## In one sentence

Vector search finds things that *mean* the same; BM25 finds things that *say*
the same; code search needs both, because bug reports contain both prose and
exact symbol names.

## The problem it solves

Two queries, two failures.

**Query: "crashes when the table doesn't exist"**
Keyword search finds nothing — the code says `raise NoTable`, sharing no words.
Vector search finds it, because the meanings are close.

**Query: `rows_where`**
Vector search returns things *about querying rows*, which may not include the
function literally called `rows_where`. An identifier is a token, not a concept.
Keyword search finds it instantly.

A real GitHub issue contains both: a prose description *and* a traceback with
exact names. Retrieving on one of those throws away half the query.

## How it works, step by step

### BM25, in plain terms

"Best Match 25" scores a document against a query by asking three questions for
each query term:

1. **How often does the term appear here?** More is better — with diminishing
   returns, so a document repeating a word fifty times does not beat everything.
2. **How rare is the term across the corpus?** Rare terms are informative.
   `self` appears in every Python file and tells you nothing; `rows_where`
   appears in one and tells you a lot. (This is the "inverse document frequency"
   idea.)
3. **How long is this document?** A term in a short function means more than the
   same term in a thousand-line file.

No machine learning. A scoring formula from the 1990s that remains extremely
hard to beat on exact terms.

### Tokenising code for BM25

Ordinary word splitting is wrong for code. `rows_where` is one word, and someone
searching "rows with a where clause" should still find it.

So identifiers are kept **whole and split**:

```
rows_where   →  ["rows_where", "rows", "where"]
getUserName  →  ["getusername", "get", "user", "name"]
```

Keeping the whole identifier preserves the exact match. Adding the parts catches
the paraphrase. Keeping *only* the parts would make `rows_where` and
`where_rows` identical, which is exactly the precision you were using BM25 for.

(One detail that caused a bug here: the camelCase split must run on the
*original* casing. Lowercasing first destroys the boundary, and `getUserName`
silently becomes a single token.)

### Fusing two rankings that are not comparable

Dense similarity is a cosine in [-1, 1]. BM25 is unbounded and depends on the
corpus. Adding them is meaningless. Normalising them requires knowing each
distribution, which changes per query.

**Reciprocal Rank Fusion** avoids the problem by throwing away the scores and
using only the positions:

```
score(chunk) = Σ over rankings of   1 / (k + rank)          k = 60
```

Worked example — a chunk ranked 1st by dense and 8th by BM25:

```
1/(60+1) + 1/(60+8) = 0.0164 + 0.0147 = 0.0311
```

versus a chunk ranked 3rd by both:

```
1/(60+3) + 1/(60+3) = 0.0159 + 0.0159 = 0.0317
```

The consistent one narrowly wins — which is the intended behaviour. Agreement
between two independent methods is evidence; one method's enthusiasm is not.

**What `k` does.** Without it, first place would be worth twice second place,
and whichever method happened to be confident would dominate. `k = 60` flattens
the curve so rank differences matter but do not overwhelm.

### Retrieving wider than you need

Each method contributes `limit × 4` candidates before fusion. A chunk ranked
15th by dense search and 2nd by BM25 should be able to surface — and it cannot
if dense search only returned 10.

## In PatchPilot

[`rag/retrieval.py`](../../src/patchpilot/rag/retrieval.py). Retrieval mode is a
parameter (`dense` / `sparse` / `hybrid`) specifically so the benchmark can
compare them. "Hybrid is better" is a claim, and the evaluation has to be able
to test it rather than assume it.

**A stated scaling limit:** BM25 here is computed in-process over the whole
corpus and cached per repository. For one repository of a few thousand chunks
that is milliseconds and a few megabytes. It does not survive many large
repositories, and the fix is Qdrant's own sparse-vector support, which keeps the
inverted index in the database. That is a deliberate deferral, recorded rather
than hidden.

## What goes wrong

**Normalising scores instead of using ranks.** Tempting, and fragile: the
normalisation depends on the score distribution, which changes per query.

**Filtering after ranking.** If you retrieve 10 and then discard the tests, you
return fewer than 10. Filter first — the same discipline the vector store uses.

**Forgetting BM25 needs the corpus.** Unlike vector search, BM25 scores against
corpus-wide statistics, so it needs the whole set of documents, not an index it
can query. That is the constraint behind the scaling limit above.

**Assuming hybrid always wins.** It usually does. It is still a measurement, not
an axiom.

## Interview questions

**Q: Why hybrid search instead of just vectors?**

Because they fail in opposite directions. Vector search handles paraphrase —
"crashes when the table doesn't exist" finds `raise NoTable` — and is weak on
exact identifiers, since `rows_where` embeds near "things about querying rows"
rather than to the function of that name. BM25 is the reverse. A GitHub issue
contains prose *and* a traceback with exact symbols, so using one method
discards half the query.

**Q: What is BM25 actually doing?**

Scoring term overlap with three adjustments: term frequency with diminishing
returns, inverse document frequency so rare terms count more than common ones,
and length normalisation so a hit in a short function outweighs the same hit in
a huge file. It is a formula, not a model — no training, no embedding, and still
very hard to beat on exact terms.

**Q: How do you combine two rankings with incomparable scores?**

Reciprocal Rank Fusion: discard the scores, sum 1/(k + rank) across rankings
with k = 60. Nothing needs normalising, and a ranker producing wild scores
cannot dominate — only its ordering counts. The constant flattens the curve so
first place is not worth disproportionately more than second.

**Q: How do you tokenise code for keyword search?**

Keep identifiers whole *and* add their parts: `rows_where` becomes
`rows_where`, `rows`, `where`. The whole form preserves exact matching; the
parts catch paraphrases. Splitting camelCase has to happen before lowercasing —
I had that bug, and it silently turned `getUserName` into one opaque token.

**Q: What breaks at scale?**

My BM25 index is in-process and holds the corpus in memory, rebuilt per
repository. Fine for one repository of a few thousand chunks; it does not
survive many large ones. The fix is sparse vectors stored in Qdrant, so the
inverted index lives in the database rather than in my process. I deferred it
deliberately and wrote down where the limit is.

## Official documentation

- [Qdrant — Hybrid queries](https://qdrant.tech/documentation/concepts/hybrid-queries/) — fusion, including RRF, as the database implements it.
- [`rank_bm25`](https://github.com/dorianbrown/rank_bm25) — the implementation used here, with the formula variants.
