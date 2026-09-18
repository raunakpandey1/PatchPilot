# Measured numbers

Every number here came from a command that was actually run. The command is
next to the number so you can re-run it and get your own.

Nothing in this file is estimated, rounded up, or borrowed from a blog post.

---

## Phase 1 — Clone strategies

**Command:** `poetry run python scripts/benchmark_clone.py <owner/name>`
**When:** 2026-09-17 · **Machine:** M1 MacBook Air, 8 GB · **Network:** home broadband

### simonw/sqlite-utils (our benchmark target)

| strategy | seconds | size MB | files | commits readable |
|---|---:|---:|---:|---:|
| full | 1.45 | 3.8 | 107 | 200 |
| shallow | 0.70 | 1.8 | 107 | **1** |
| blobless | 1.19 | 2.5 | 107 | 200 |

### pallets/flask

| strategy | seconds | size MB | files | commits readable |
|---|---:|---:|---:|---:|
| full | 1.17 | 14.5 | 236 | 200 |
| shallow | 0.68 | 2.6 | 236 | **1** |
| blobless | 1.41 | 6.5 | 236 | 200 |

### psf/requests

| strategy | seconds | size MB | files | commits readable |
|---|---:|---:|---:|---:|
| full | 1.05–1.18 | 17.6 | 130 | 200 |
| shallow | 0.73 | 7.3 | 130 | **1** |
| blobless | 1.41–1.47 | 10.7 | 130 | 200 |

*(`requests` timings are the range across four runs. One earlier blobless run
took 3.91 s; three repeats landed at 1.41–1.47 s, so that first figure was an
outlier — probably a cold pack on GitHub's side. It is recorded here rather
than discarded, because throwing away inconvenient measurements is how you end
up believing things that are not true.)*

### What these numbers say

- **Shallow reads one commit of history.** That is not a small caveat: it means
  no `git log`, no `git blame`, no "which commit introduced this". Phase 4 needs
  all three, so shallow is disqualified regardless of how fast it is.
- **Blobless saves real disk and the saving grows with history** — 34% on
  sqlite-utils, 55% on flask, 39% on requests. The bigger the history, the more
  it saves, because history is mostly old file contents nobody reads.
- **Blobless costs about 20–35% more wall-clock time** on these repositories.
  The server has to compute a filtered pack rather than send a prepared one.
  Honest tradeoff: we pay a little time for disk, and keep history.

---

## Phase 1 — Conditional requests (ETags)

**Command:** `poetry run python scripts/benchmark_github.py`
**When:** 2026-09-17 · **Target:** `simonw/sqlite-utils`, authenticated

| run | issues | seconds | HTTP requests | 304s | rate-limit budget spent |
|---|---:|---:|---:|---:|---:|
| cold cache | 77 | 1.03 | 3 | 0 | **2** |
| warm cache | 77 | 1.09 | 3 | 3 | **0** |

### What this number says

A repeated fetch of the same data costs **zero** rate-limit budget. GitHub's
documented behaviour — that a `304 Not Modified` is not charged — holds in
practice.

Concretely, on a 5,000 requests/hour budget: re-checking this repository's
issues costs 2 requests without caching and 0 with it. The limit stops being
the thing that decides how many repositories can be tracked.

*(The first version of this measurement showed 61 issues on the warm run
instead of 77. That was a real bug — see [failures.md](failures.md) F-002. The
numbers above are from after the fix.)*

---

## Phase 1 — Test suite

**Command:** `poetry run pytest -m "not integration" -q`

| metric | value |
|---|---|
| unit tests | 69 |
| unit suite runtime | ~2 s |
| network calls in unit suite | 0 |
| integration tests | 6 |
| `mypy --strict` errors | 0 |
| `ruff check` errors | 0 |

Runtime matters more than it looks: a suite that takes two seconds gets run on
every change. One that takes two minutes gets run before commits. One that
needs the network gets skipped.

---

## Not yet measured

Listed so the gaps are explicit rather than quietly missing:

- RAG Recall@5 / MRR (Phase 3)
- Root-cause accuracy (Phase 4)
- First-attempt fix rate, iterations to success (Phase 7)
- Tokens, latency and cost per run (Phase 10)
- Unsafe-action block rate (Phase 8)
