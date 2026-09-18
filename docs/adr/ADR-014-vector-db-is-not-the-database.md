# ADR-014 — The vector database is not the primary database

**Status:** Accepted · **Phase:** 3 · **Date:** 2026-09-18

## Context

PatchPilot now has Qdrant holding chunks and their vectors, and SQLite holding
LangGraph checkpoints. Later phases add agent runs, patches, test results and
approvals.

An obvious simplification presents itself: Qdrant stores arbitrary JSON payloads
alongside vectors. Why not keep everything there and run one datastore?

## Options considered

1. **Everything in Qdrant.** One system, one dependency.
2. **Everything in a relational store**, with vectors via pgvector.
3. **Split by responsibility** — vectors in Qdrant, application state in SQLite
   (Postgres if this ever scales).

## Decision

**Split by responsibility.**

| Store | Holds | Answers |
|---|---|---|
| Qdrant | chunks + vectors | *"what looks similar to this?"* |
| SQLite | checkpoints, runs, patches, approvals | *"what is true, and what happened?"* |

## Reasoning

They answer different kinds of question, and the difference is not stylistic.

**A vector search is approximate and ranked.** "The ten nearest chunks" is a
best-effort ordering from an index that may miss a true nearest neighbour. That
is exactly right for retrieval and exactly wrong for *"which runs did this user
approve, and what did they cost?"* — a question with one correct answer that
must not be approximate.

**Vector databases have no transactions.** "Record the approval and mark the run
complete" must either both happen or neither. Without transactions, a crash
between the two leaves a run that was approved but not marked, or worse.

**No joins, no foreign keys.** "Every patch for issues in repositories I own" is
a relational question. In a payload store it becomes several queries and
application-side joining, plus nothing preventing a patch pointing at a run that
does not exist.

**Different lifecycles.** The index is derived data — delete it and re-index and
you have lost nothing but time. Approvals and run history are the record; losing
them loses the thing itself. Conflating a cache with a system of record is how
you end up unable to say which one you are allowed to delete.

**Why not pgvector, then?** It is the coherent version of option 2 and a
legitimate choice. It needs Postgres running, which the 8 GB budget rules out,
and it couples two workloads with different scaling shapes: the vector index
grows with code volume and is rebuilt wholesale, while the transactional tables
grow with usage and must never be rebuilt. Keeping them separate means either
can be scaled or replaced alone.

## Tradeoffs

**Against:**

- **Two systems** to configure, back up and reason about.
- **No cross-store transaction.** Writing a run record and deleting its chunks
  cannot be atomic, so the code must tolerate a half-completed pair.
- **Consistency is the application's problem.** If a repository is deleted from
  SQLite, nothing automatically removes its chunks from Qdrant.

**For:** each store used for what it is good at; the index is disposable and
known to be disposable; either side can be replaced independently.

## Consequences

- Chunks in Qdrant carry `repository` so they can be deleted as a set when
  application state changes.
- The index is treated as derived: any inconsistency is resolved by re-indexing,
  never by hand-editing Qdrant.
- Phase 11's benchmark results go to SQLite, not Qdrant — they are records, not
  retrievable text.

## Interview questions

**Q: Why not store everything in the vector database?**

Because they answer different questions. Vector search is approximate and ranked
— ideal for "what looks similar", wrong for "which runs did this user approve",
which has exactly one correct answer. Vector stores also lack transactions,
joins and foreign keys, so "record the approval and mark the run complete"
cannot be made atomic and nothing stops a patch referencing a run that does not
exist.

**Q: What is the difference in lifecycle?**

The index is derived data: delete it, re-index, lose nothing but time. Approvals
and run history *are* the data — losing them loses the thing itself. Keeping
them in one store makes it ambiguous which you are allowed to delete, and that
ambiguity is how people accidentally destroy records while clearing a cache.

**Q: What does the split cost you?**

Cross-store consistency becomes the application's problem. Deleting a repository
from SQLite does not remove its chunks from Qdrant, and the two writes cannot be
one transaction. I handle it by treating the index as disposable — any
inconsistency is fixed by re-indexing — which is only a safe answer because the
index really is derived.

**Q: Would pgvector solve this?**

It is the coherent alternative and it does give you one transactional system. It
needs Postgres running, which does not fit an 8 GB machine also running Docker,
and it couples two workloads with different shapes — the vector index grows with
code volume and gets rebuilt wholesale, the transactional tables grow with usage
and must never be rebuilt. At a scale where I could run Postgres properly I
would genuinely reconsider.

## Behavioural question this answers

> *"Tell me about a time you argued against simplifying something."*

Once Qdrant was storing JSON payloads next to vectors, the obvious
simplification was to keep everything there and run one datastore instead of
two. I argued against it on the grounds that the two stores answer different
kinds of question: vector search is approximate and ranked, which is right for
"what looks similar" and wrong for "which runs were approved" — a question with
one correct answer, needing transactions and foreign keys that a vector store
does not have. The clearest version of the argument was lifecycle: the index is
derived data I can delete and rebuild, and the approval history is the record.
One store would have made it ambiguous which was which, and that ambiguity is
how someone eventually deletes the wrong thing.
