# ADR-011 — Qdrant in embedded mode for vector storage

**Status:** Accepted · **Phase:** 3 · **Date:** 2026-09-18

## Context

Repository RAG needs to store a few thousand chunk vectors per repository and
retrieve the nearest ones to a query — filtered by repository, by whether a
chunk is a test, by language, and sometimes by file.

The machine is an 8 GB M1 MacBook Air that must also run Docker for the Phase 6
sandbox. There is no budget for a hosted service.

## Options considered

1. **A list of vectors in memory**, compared exhaustively per query. Exact, zero
   dependencies. No persistence, no metadata, no filtering.
2. **FAISS.** Mature, fast approximate search.
3. **Chroma.** Purpose-built for this use case, very simple API.
4. **pgvector.** Vectors inside Postgres.
5. **Qdrant, embedded** (`QdrantClient(path=...)`).
6. **Pinecone / Weaviate Cloud.** Hosted.

## Decision

**Qdrant in embedded mode.**

## Reasoning

Two properties of this project decide it.

**1. Embedded mode with a server-identical API.** Qdrant runs as a library
writing to a local folder — no server, no Docker, no extra gigabyte of RAM. That
is not a nice-to-have here.

The part that makes it more than a convenience is that the embedded client and
the server client are the *same API*. "How would you scale this?" is answered by
changing a constructor argument, not by rewriting retrieval. An architecture
whose escape route is one line is a different thing from one whose escape route
is a migration.

**2. Filtering is first-class, and filtering is most of what we do.** Retrieval
here is never purely semantic. Every real query is *"chunks from this
repository, that are source code not tests, in Python, similar to this issue
text."*

That distinction matters more than it sounds:

- **Post-filtering** — retrieve 50, discard the non-matching ones. If 45 of the
  50 nearest chunks are tests, you asked for 10 and get 5. Recall silently
  drops, and the number you publish is wrong.
- **Pre-filtering** — narrow to matching chunks first, then search within them.
  You get the 10 best results that satisfy the filter.

Qdrant pre-filters. **Verified rather than assumed**: store 30 test chunks that
match a query well plus 4 source chunks that match poorly, then request 4
results excluding tests. Post-filtering would return zero; embedded Qdrant
returns 4 source chunks. That experiment is a test —
`test_filters_are_applied_during_search_not_after`.

## Why not the alternatives

| Option | Why not |
|---|---|
| A list + exhaustive comparison | Exact and simple, but no persistence, no metadata, no filtering — you end up writing a database around it. A reasonable starting point that we would have had to leave |
| FAISS | An index, not a database. No payloads, no metadata filtering, no persistence story. We would hand-build the surrounding store, which is the part that actually matters here |
| Chroma | The closest competitor and simpler to start with. Weaker filtered-search performance, a less stable API across versions, and no clean local→server continuity — which is the property doing the most work in this decision |
| pgvector | Requires Postgres running, which the 8 GB budget rules out. It is also the anti-pattern we deliberately demonstrate avoiding: transactional and vector workloads have different shapes (ADR-014) |
| Pinecone / Weaviate Cloud | Cost money, and add a network round-trip to every retrieval — including every one of the thousands made while benchmarking |

## Tradeoffs

**Against:**

- **Payload indexes do nothing in embedded mode.** The client says so, with a
  warning. Discovered while building this, and worth stating precisely: the
  *correctness* property (pre-filtering) holds, the *acceleration* does not. So
  filtered search is right but scans payloads, which is a scale concern rather
  than a correctness one. Index creation is skipped in local mode rather than
  left in to imply a guarantee we do not have.
- **Single process.** Embedded Qdrant is one writer. Concurrent agents would
  contend.
- **A Rust binary dependency** and a larger API surface than Chroma's.
- **Approximate search.** HNSW can miss a true nearest neighbour. For this use
  case that is an acceptable trade and is captured in the recall measurement
  rather than assumed away.

**For:** no server, no memory overhead, real metadata filtering, persistence
across processes, and a migration path that is a constructor argument.

## Consequences

- All Qdrant-specific code lives in
  [`rag/store.py`](../../src/patchpilot/rag/store.py); the filter vocabulary is
  translated in one function, so nothing else in the codebase constructs a
  Qdrant object.
- Tests use `QdrantClient(":memory:")`, exercising the same code paths.
- Concurrency is out of scope until the store changes — a limit stated rather
  than discovered.
- BM25 is *not* in Qdrant yet; it is computed in-process (ADR-012), which is the
  first thing to move if this has to scale.

## Interview questions

**Q: Why Qdrant?**

Two reasons specific to this project. It runs embedded — a library writing to a
folder, no server, which matters on an 8 GB machine also running Docker — while
exposing the same API as the server, so scaling later is a constructor change
rather than a rewrite. And its metadata filtering is first-class and applied
before the vector search, which matters because every query here is filtered by
repository, language and test/non-test.

**Q: Why does pre-filtering versus post-filtering matter so much?**

Because post-filtering silently destroys recall. If you retrieve the 50 nearest
and then discard tests, and 45 of them were tests, you asked for 10 results and
got 5 — with no error. Since Recall@K is the headline number I report, a
retrieval layer that quietly returns fewer results than requested would make the
number wrong rather than just worse. I verified Qdrant pre-filters with a
constructed case rather than trusting the documentation.

**Q: What did you find out about embedded mode that surprised you?**

Payload indexes have no effect there — the client warns about it. I checked what
that actually costs: filtering is still applied during the search, so
correctness holds; what is missing is the acceleration, which is a scale concern.
I skip index creation in local mode rather than leave code in that implies a
guarantee I do not have, and I wrote the finding down.

**Q: When would you switch to something else?**

Concurrency or corpus size. Embedded Qdrant is single-process, so several agents
indexing at once will contend, and without payload indexes filtered search scans
payloads. Both point at the same move — a Qdrant server — which is why the
embedded/server API parity was the deciding property rather than a bonus.

## Behavioural question this answers

> *"Tell me about a time you verified an assumption instead of trusting the
> documentation."*

I chose Qdrant partly because it pre-filters — narrowing by metadata before the
vector search rather than discarding results afterwards, which silently costs
recall. Then I noticed a warning that payload indexes have no effect in embedded
mode, which is the mode I am using. Rather than assume that was fine, I built the
case that would distinguish the two behaviours: thirty test chunks matching the
query strongly, four source chunks matching weakly, then ask for four results
excluding tests. Post-filtering returns nothing; it returned four. So the
correctness property held and only the acceleration was missing. That is now a
test, and the distinction is written down in the ADR — because "Qdrant filters
efficiently" and "Qdrant filters correctly" are different claims and I was only
entitled to one of them.
