# PatchPilot documentation

These docs are written for someone who knows nothing about agents, RAG or
LangGraph. Nothing assumes you have read anything else first, and every term is
defined before it is used.

## Start here

1. **[concepts/](concepts/)** — the ideas, from zero. Read these when you meet a
   word you do not know.
2. **[journey/](journey/)** — the build diary. One file per phase: what we built,
   why, what broke, and how it was fixed.
3. **[adr/](adr/)** — Architecture Decision Records. Every real decision, the
   options that were actually considered, and why one won.
4. **[interview/](interview/)** — questions and answers, and STAR stories built
   from what really happened while building this.
5. **[failures.md](failures.md)** — the failure log. Real bugs only.
6. **[metrics.md](metrics.md)** — every measured number, with the command that
   produced it.

## Two rules these docs follow

**No invented numbers.** Every figure in `metrics.md` comes from a command you
can re-run. If something was not measured, it says so.

**No invented failures.** `failures.md` records bugs that actually happened
during the build, with the real symptom and the real root cause.

## Reading order by phase

| Phase | Journey | Concepts introduced | ADRs |
|---|---|---|---|
| 0 — Foundations | [phase-00](journey/phase-00-foundations.md) | [project layout](concepts/python-project-layout.md), [configuration](concepts/configuration-and-secrets.md), [structured logging](concepts/structured-logging.md) | [001](adr/ADR-001-src-layout.md), [002](adr/ADR-002-pydantic-settings.md), [003](adr/ADR-003-structlog.md) |
| 1 — GitHub & repos | [phase-01](journey/phase-01-github-and-repository-analysis.md) | [REST APIs & rate limits](concepts/rest-apis-and-rate-limits.md), [the git object model](concepts/git-object-model.md), [testing against third-party APIs](concepts/testing-against-third-party-apis.md), [untrusted input](concepts/untrusted-input.md) | [004](adr/ADR-004-blobless-clone.md), [005](adr/ADR-005-rest-etag-over-graphql.md), [006](adr/ADR-006-deterministic-detection.md) |
| 2 — LangGraph & LLM | [phase-02](journey/phase-02-langgraph-and-llm-abstraction.md) | [what an LLM is](concepts/what-is-an-llm.md), [what an agent is](concepts/what-is-an-agent.md), [state, nodes, edges](concepts/langgraph-state-nodes-edges.md), [checkpointing](concepts/checkpointing-and-resume.md), [structured output](concepts/structured-output.md), [provider abstraction](concepts/llm-provider-abstraction.md) | [007](adr/ADR-007-langgraph.md), [008](adr/ADR-008-llm-provider-abstraction.md), [009](adr/ADR-009-sqlite-checkpointer.md), [010](adr/ADR-010-deterministic-ranking.md) |
| 3 — Repository RAG | [phase-03](journey/phase-03-repository-rag.md) | [RAG end to end](concepts/rag-end-to-end.md), [embeddings & vectors](concepts/embeddings-and-vectors.md), [chunking code](concepts/chunking-code.md), [BM25 & hybrid search](concepts/bm25-and-hybrid-search.md), [reranking](concepts/reranking.md), [retrieval evaluation](concepts/retrieval-evaluation.md) | [011](adr/ADR-011-qdrant.md), [012](adr/ADR-012-hybrid-retrieval.md), [013](adr/ADR-013-code-aware-chunking.md), [014](adr/ADR-014-vector-db-is-not-the-database.md) |
