# ADR-009 — SQLite for checkpoints, not Postgres

**Status:** Accepted · **Phase:** 2 · **Date:** 2026-09-18

## Context

LangGraph writes agent state to a checkpointer after every step. That store is
what makes Phase 9's human approval possible — the run stops, the process exits,
and a later invocation resumes the same state — and what stops a crash from
costing a whole run's model calls.

The development machine is an 8 GB M1 MacBook Air that will also be running
Docker for the Phase 6 sandbox.

## Options considered

1. **In-memory** — nothing persists. Fine for tests, useless for approval.
2. **SQLite** — a file. No server, no configuration.
3. **Postgres** — a server. Concurrent writers, real operational tooling.
4. **Redis** — fast, and the wrong durability model for something that must
   survive a laptop sleeping.

## Decision

**SQLite** for real runs, **in-memory** for tests. Both are used today.

## Reasoning

The properties actually required are: survives process exit, keyed by run id,
holds per-step history. SQLite has all three in a file.

What Postgres adds is concurrency and operational tooling for many simultaneous
writers. PatchPilot runs one agent at a time on a laptop. Paying a gigabyte of
RAM and a server process for concurrency we do not use, on a machine where
Docker also needs memory, is a bad trade.

The migration path is real rather than theoretical: `PostgresSaver` implements
the same interface, so moving is a constructor change in one function. That is
also the honest answer to "how would you scale this?" — not a rewrite.

Using the in-memory checkpointer in tests is deliberate. It exercises the same
resume machinery without touching the filesystem, so the tests assert the
behaviour rather than a particular storage backend.

## Tradeoffs

**Against:**

- **One writer.** SQLite locks on write. Multiple agents against one file will
  contend, so this does not survive concurrency.
- **Local only.** A checkpoint on your laptop is not readable by a worker
  elsewhere, which rules out distributed execution.
- **No operational tooling.** No replication, no managed backups.

**For:** zero setup, zero memory overhead, a single file to delete when the
state schema changes during development, and an interface identical to the
production-grade option.

## Consequences

- Deleting `workspace/checkpoints.sqlite` loses the ability to resume paused
  runs and nothing else. It holds no other state.
- Phase 13's deployment cannot share checkpoints between the Hugging Face Space
  and anything else. Either state stays within one process, or that is the
  moment to move to Postgres.
- Concurrency is out of scope until the storage changes — a limit worth stating
  rather than discovering.

## Interview questions

**Q: Why SQLite and not Postgres?**

The requirements are: survive process exit, key by run id, keep per-step
history. SQLite does all three in a file, with no server and no memory overhead
— which matters on an 8 GB machine that also runs Docker. What Postgres adds is
concurrent writers, and this runs one agent at a time.

**Q: What breaks when you need concurrency?**

SQLite serialises writers, so several agents against one file will contend and
eventually time out. That is the point to switch. Because `PostgresSaver`
implements the same interface, it is a change in one constructor rather than a
rewrite — which is the main reason I did not hand-roll the storage.

**Q: Why use a different checkpointer in tests?**

The in-memory one exercises identical resume machinery without touching the
filesystem, so tests stay fast and leave nothing behind. The important assertion
— that a *different graph object* can read back a completed run's state — is
about the persistence contract, not about which backend implements it.

**Q: Is the checkpoint database a cache?**

No, and conflating them causes bugs. It records what happened in one specific
run, keyed by thread id. It is not shared between runs and does not deduplicate
work across them. Caching repository data is a separate concern with a separate
store.

## Behavioural question this answers

> *"Tell me about a time you deliberately chose the less powerful option."*

Postgres is the obvious production choice for agent checkpoints, and I chose
SQLite. The requirements were survive-process-exit, key-by-run-id, and per-step
history — all of which a file gives you — while Postgres would have added a
server process and about a gigabyte of RAM on a machine that also has to run
Docker. The part that made it safe was checking the exit: the Postgres
checkpointer implements the same interface, so moving is one constructor call. I
would rather take the smaller thing and know precisely what would force the
change than take the bigger one because it sounds more serious.
