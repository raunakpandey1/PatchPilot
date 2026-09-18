# ADR-004 — Blobless clone as the default strategy

**Status:** Accepted · **Phase:** 1 · **Date:** 2026-09-17

## Context

PatchPilot clones repositories to read their code. Later phases need more than
the current files:

- **Phase 3 (RAG)** indexes source files.
- **Phase 4 (root cause)** needs `git log` and `git blame` to answer "when did
  this change, and why".
- **Phase 3 evaluation** needs `git show --name-only` on the commit that closed
  an issue, to know which files actually had to change — free ground truth for
  measuring retrieval.
- **Phase 5** creates branches and commits.

The machine has 8 GB of RAM and disk is not unlimited. The repository is
untrusted input, so time and size must be bounded.

## Options considered

1. **Full clone** — everything. Complete, largest, slowest.
2. **Shallow** (`--depth=1`) — the latest commit and its files. Smallest and
   fastest; **no history**.
3. **Blobless** (`--filter=blob:none`) — all commits and trees, file contents
   fetched on demand. Full history at a fraction of the size.
4. **Treeless** (`--filter=tree:0`) — smaller still, but most history operations
   trigger a fetch, making `log` over paths slow.
5. **Tarball via the GitHub API** — no git at all. No history, and it spends
   API rate limit that we would rather keep for issues.

## Decision

**Blobless** (`--filter=blob:none`), with `--single-branch`, `--no-tags`, no
submodule recursion, a wall-clock timeout, and a size budget.

## Reasoning

Shallow is eliminated on capability, not on cost. Measured
([metrics.md](../metrics.md)): a shallow clone of `pallets/flask` reads **1**
commit versus 200 for the other strategies. Phases 3 and 4 both depend on
history, so the cheapest option is also the one that cannot do the job.

Between full and blobless, the measurements:

| repo | full | blobless | saving | history |
|---|---:|---:|---:|---|
| sqlite-utils | 3.8 MB | 2.5 MB | 34% | same |
| flask | 14.5 MB | 6.5 MB | 55% | same |
| requests | 17.6 MB | 10.7 MB | 39% | same |

Blobless keeps everything we need and skips what we do not: historical versions
of files nobody will read. The saving grows with the age of the repository,
because that is exactly where the unread blobs accumulate.

## Tradeoffs

**Against:**
- **Slower.** Measured 20–35% more wall-clock time on these repositories — the
  server computes a filtered pack rather than sending a prepared one.
- **Lazy fetches.** Reading an old file version triggers a network round trip.
  `GitRepository.show_file()` may be slow the first time, and requires the
  network to still be reachable.
- **Not offline-complete.** A full clone works with no network afterwards; a
  blobless one may not.

**For:** full history at roughly half the disk; the operations every later phase
needs all work.

## Consequences

- Phase 6's sandbox must copy the working tree, not rely on the object store.
- If lazy fetch latency becomes a problem in Phase 3, the fallback is a full
  clone for the single benchmark repository — the strategy is a parameter.
- Clones are budgeted: `timeout_s` and `max_size_mb`, both enforced.
- `protocol.file.allow=never` by default; tests opt in explicitly (see
  [failures.md](../failures.md) F-003).

## Interview questions

**Q: Why not `--depth=1`? It's the fastest.**

Because it removes history. `git log` returns one commit and `git blame` cannot
attribute a line to the change that introduced it. Root-cause analysis depends
on both, and our retrieval evaluation depends on reading the commit that closed
an issue. Measured: shallow reads 1 commit, blobless 200, at 2.6 MB versus
6.5 MB on flask. The 4 MB is worth it.

**Q: What does `--filter=blob:none` actually skip?**

Blobs — file contents. You still get every commit and tree, so the shape and
history of the repository are complete; what is missing is the content of files
at old commits, fetched on demand if you ask for one. For tooling that reads
current code and historical *metadata*, that is nearly all of the size and none
of the capability.

**Q: What does it cost?**

Time and offline completeness. We measured 20–35% slower clones, because the
server computes a custom pack. And reading an old file version requires a
network round trip, so a blobless clone is not fully usable offline.

**Q: How do you stop a hostile repository filling your disk?**

A wall-clock timeout that kills the clone, and a size budget checked afterwards
that deletes it and raises. The timeout is the real defence — git gives no
reliable way to abort partway on size — so the size check catches the merely
too big rather than the pathological.

## Behavioural question this answers

> *"Tell me about a time you made a decision based on data rather than
> intuition."*

The intuitive choice for cloning repositories is `--depth=1`: it is the fastest
and smallest, and most tooling uses it. I wrote a benchmark comparing three
strategies on three real repositories and measured both size and a capability —
how many commits remained readable. Shallow was smallest and returned one commit
of history, which would have broken root-cause analysis two phases later.
Blobless kept full history at 45% of a full clone's size, for 20–35% more time.
The numbers are in the repository and the benchmark is re-runnable.
