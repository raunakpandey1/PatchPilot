# Architecture Decision Records

One file per real decision. Each records what was actually considered at the
time, not a justification written afterwards.

Each ADR ends with two sections that exist for interview preparation:

- **Interview questions** — what a technical interviewer would ask about this
  decision, with answers.
- **Behavioural question this answers** — the "tell me about a time when…"
  question this decision is genuine evidence for.

| # | Decision | Phase |
|---|---|---|
| [001](ADR-001-src-layout.md) | `src/` layout | 0 |
| [002](ADR-002-pydantic-settings.md) | pydantic-settings for configuration; token optional | 0 |
| [003](ADR-003-structlog.md) | structlog for event-style logging | 0 |
| [004](ADR-004-blobless-clone.md) | Blobless clone as the default strategy | 1 |
| [005](ADR-005-rest-etag-over-graphql.md) | REST + ETag caching instead of GraphQL | 1 |
| [006](ADR-006-deterministic-detection.md) | Deterministic repository analysis, no LLM | 1 |
| [007](ADR-007-langgraph.md) | LangGraph for orchestration | 2 |
| [008](ADR-008-llm-provider-abstraction.md) | LLM provider behind a Protocol | 2 |
| [009](ADR-009-sqlite-checkpointer.md) | SQLite checkpoints, not Postgres | 2 |
| [010](ADR-010-deterministic-ranking.md) | Deterministic issue ranking | 2 |
| [011](ADR-011-qdrant.md) | Qdrant in embedded mode | 3 |
| [012](ADR-012-hybrid-retrieval.md) | Hybrid retrieval with rank fusion | 3 |
| [013](ADR-013-code-aware-chunking.md) | AST chunking, not fixed-size | 3 |
| [014](ADR-014-vector-db-is-not-the-database.md) | The vector DB is not the primary database | 3 |
