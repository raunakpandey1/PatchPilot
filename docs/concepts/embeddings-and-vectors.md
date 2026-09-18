# Embeddings, vectors, and cosine similarity

## In one sentence

An embedding model turns text into a list of numbers positioned so that texts
with similar meanings end up close together — and "close" is measured by the
angle between them.

## The problem it solves

A bug report says *"crashes when the table doesn't exist"*. The code says:

```python
if not self.exists():
    raise NoTable(self.name)
```

Not one word in common. A keyword search finds nothing. But the two clearly mean
the same thing, and you want a search that knows that.

Embeddings are how. They convert "meaning" into geometry, and geometry is
something a computer can measure.

## How it works, step by step

### 1. Text becomes a point in space

```
"crashes when the table doesn't exist"
        ↓  embedding model
[0.21, -0.04, 0.88, ..., 0.13]      ← 384 numbers
```

384 numbers is a point in 384-dimensional space. You cannot picture that, and
you do not need to — the same rules apply as in two dimensions, which you can
picture.

### 2. Closeness is the angle, not the distance

Imagine two arrows from the origin. If they point the same way, the texts mean
similar things. How *long* the arrows are should not matter — a long document
and a short one about the same topic should still match.

So similarity is the **cosine of the angle** between them:

```
cos  ≈  1.0   →  same direction     — similar meaning
cos  ≈  0.0   →  perpendicular      — unrelated
cos  ≈ -1.0   →  opposite           — contradictory
```

Our vectors are normalised to length 1, so the cosine reduces to the dot
product — multiply the pairs and add:

```python
def cosine_similarity(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (norm(a) * norm(b))
```

That is written out in
[`rag/embeddings.py`](../../src/patchpilot/rag/embeddings.py) rather than
imported, so the arithmetic behind "vector search" is visible in the repository.

### 3. Why the model puts related things close together

The model was trained on pairs of texts known to be related — a question and the
passage that answers it, a sentence and its paraphrase — and adjusted until
related pairs scored high and unrelated pairs scored low. Millions of examples
later, "similar meaning" and "small angle" coincide.

This matters for one practical reason:

### 4. Queries and documents are embedded *differently*

BGE models are trained so that a **question** and the **passage answering it**
land close. A question and a passage are different kinds of text, so the model
needs to know which it is being given — the query gets a short instruction
prefix, the passage does not.

```python
embeddings.embed_query("why does rows_where fail?")      # query mode
embeddings.embed_documents(["def rows_where(...): ..."]) # passage mode
```

Embedding a query as though it were a document is a **silent** quality loss: the
code runs, results are just worse. Two distinct methods keep the distinction
visible.

### 5. Dimensions, and why 384

More dimensions can encode more nuance, and cost more memory and time. BGE-small
uses 384 and is 67 MB; large models use 1024+ and are gigabytes. For one
repository on an 8 GB laptop, small wins — and it can be swapped, because the
dimension is a constructor argument.

## Where embeddings are weak

This is the half people skip, and it is why retrieval here is
[hybrid](bm25-and-hybrid-search.md).

**Exact identifiers.** Search for `rows_where` and the model returns things
*about querying rows* — which may not include the function literally called
`rows_where`. An identifier is a token, not a concept.

**Rare words.** A project-specific term appearing nowhere in training data has
no meaningful position.

**Negation.** "Does not raise an error" and "raises an error" embed close
together. The vector captures the topic, not the polarity.

**Long text.** Everything over the model's limit (512 tokens for BGE-small) is
silently truncated. A chunk longer than that is partly invisible — which is a
constraint on [chunking](chunking-code.md), not a detail.

## In PatchPilot

- [`rag/embeddings.py`](../../src/patchpilot/rag/embeddings.py) —
  `LocalEmbeddings` (real, 67 MB, CPU) and `HashingEmbeddings` (for tests).
- [`rag/store.py`](../../src/patchpilot/rag/store.py) — cosine distance,
  because vectors are normalised and direction is what carries the meaning.

**On the test implementation.** `HashingEmbeddings` is not a stub returning
zeros. It is a hashing vectoriser: tokenise, hash each token into a bucket,
count, normalise. Texts sharing vocabulary genuinely come out close. That
matters — a fake returning random vectors would let a completely broken
retrieval pipeline pass its tests. It gives real, if crude, lexical similarity,
so the tests exercise actual ranking behaviour while staying offline and
instant. What it cannot do is match "crashes when the table is missing" to
`raise NoTable`; that is what the real model is for, and what the benchmark
measures.

## Interview questions

**Q: What is an embedding?**

A mapping from text to a fixed-length vector, trained so that texts with similar
meanings land close together. It turns "are these two things about the same
thing?" into a geometric question — the angle between two vectors — which is
something you can index and search efficiently.

**Q: Why cosine similarity rather than Euclidean distance?**

Because direction carries the meaning and magnitude mostly carries length. A
long document and a short one about the same topic should match, and Euclidean
distance would separate them by size. With normalised vectors the two measures
rank identically, but cosine states the intent.

**Q: Where do embeddings fail?**

Exact identifiers, negation, and rare project-specific terms. `rows_where`
embeds near "things about querying rows", which may exclude the function
actually called that. "Does not raise" sits close to "raises" because the vector
captures topic, not polarity. Both are why this project pairs vector search with
BM25 rather than relying on either.

**Q: Why run the embedding model locally?**

Indexing a repository means embedding thousands of chunks. On a hosted API that
is thousands of billed calls, and on a free tier it is a quota I need elsewhere.
The local model is 67 MB, runs on CPU, works offline — which also means the
tests can use the real thing rather than a mock.

**Q: What is the difference between embedding a query and embedding a document?**

BGE models are trained so a question and the passage answering it land close, so
the query takes a short instruction prefix and the passage does not. Getting it
wrong is a silent quality loss — nothing errors, results are just worse. The
interface exposes `embed_query` and `embed_documents` separately so the
distinction cannot be lost by accident.

## Official documentation

- [Qdrant — Vector search basics](https://qdrant.tech/documentation/concepts/search/) — distance metrics and what the index does.
- [BGE models on Hugging Face](https://huggingface.co/BAAI/bge-small-en-v1.5) — the model used here, and its query-prefix convention.
