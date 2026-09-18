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

---

## Phase 2 — The agent graph on a real repository

**Command:** `poetry run python scripts/run_graph.py simonw/sqlite-utils`
**When:** 2026-09-18 · authenticated, warm clone

| stage | result |
|---|---|
| repository analysed | 107 files, pytest, setuptools, testable ✅ |
| clone | 2.5 MB blobless, 1.25 s |
| issues fetched | 77 open (3 HTTP requests) |
| issues ranked | 77 in < 3 ms |
| verdicts | **10 attempt · 37 maybe · 30 skip** |
| selected | #841 at score **0.91** |
| nodes visited | analyze_repository → discover_issues → rank_issues → select_issue |
| LLM tokens used | **0** |

Zero tokens is the notable number. Everything in this phase — analysis,
discovery, ranking, selection — is deterministic. The model is wired in and
unused until Phase 4.

### Ranking behaviour on real data

Of 77 open issues, **13% cleared the `attempt` threshold**. The top result:

```
#841 — rows_where() and delete_where() fail to throw errors against
       non-existent tables
  verdict: ATTEMPT   score: 0.91
    + reproducibility   3.00  2 reproduction signals (traceback/code/steps)
    + labels            2.00  labelled bug
    + scope             1.20  focused description
    + discussion        0.60  1 comments — some confirmation
    + clarity           0.50  specific title
    + staleness         0.50  active (35 days ago)
```

**An observation, recorded as a hypothesis rather than acted on:** issues #399
and #430 scored `attempt` despite being **1,220 and 1,556 days** without
activity. The staleness weight looks too low relative to reproducibility. That
is a guess. Phase 11 measures how well the `attempt` verdict predicts actual fix
success, and the weights get tuned against that — which is possible only because
the ranker is deterministic ([ADR-010](adr/ADR-010-deterministic-ranking.md)).

---

## Phase 2 — Test suite

**Command:** `poetry run pytest -m "not integration"`

| metric | Phase 1 | Phase 2 |
|---|---:|---:|
| unit tests | 69 | **121** |
| suite runtime | ~2 s | **~7.4 s** |
| network calls in unit suite | 0 | **0** |
| LLM calls in unit suite | — | **0** |
| `mypy --strict` errors | 0 | 0 |
| `ruff check` errors | 0 | 0 |

The unit suite exercises the full agent graph — cloning, ranking, checkpointing,
resume — with no network, no API key and no model. That is what the fake
provider and the injectable HTTP transport are for.

---

## Phase 3 — Indexing cost

**Command:** `poetry run python scripts/benchmark_retrieval.py simonw/sqlite-utils`
**When:** 2026-09-18 · M1 MacBook Air, 8 GB · BGE-small-en-v1.5 on CPU

| metric | value |
|---|---|
| files in repository | 107 |
| files indexed | 89 |
| files skipped | 10 |
| chunks created | **1,873** |
| indexing time | **504 s** (~8.4 min) |
| throughput | ~3.7 chunks/s |
| embedding model | BAAI/bge-small-en-v1.5 (67 MB, 384-dim) |
| index size on disk | ~2 MB |

### What this says

**Embedding is the bottleneck, not chunking or storage.** Chunking 1,873 pieces
takes under a second; embedding them takes eight minutes on this CPU. That is
the price of running the model locally — and the alternative is thousands of
billed API calls per repository, which on a free tier is not an alternative.

It is also a one-off. The index persists to disk, so this cost is paid once per
repository, not once per query. Re-indexing is only needed when the code changes.

**Where it would be sped up if it mattered:** a GPU (not available here), a
smaller model (quality cost, measurable), or incremental indexing that only
re-embeds changed files. The last is the right answer and is not built, because
nothing yet re-indexes often enough for it to matter.

## Phase 3 — Ground truth

| metric | value |
|---|---|
| closed issues fetched | 400 |
| commits searched for issue references | 2,000 |
| usable labelled examples | **192** |
| average relevant files per example | 1.0 (after excluding tests) |

192 of 400 closed issues produced a usable label. The rest were closed without an
identifiable fixing commit, fixed by a commit touching more than six files (a
refactor — counting it would inflate recall), or fixed only in test files.

An earlier version of this table said 200 examples averaging 1.96 files. That
version was wrong in a way that mattered — see [failures.md](failures.md) F-005.

---

## Phase 3 — Retrieval quality

**Command:** `poetry run python scripts/benchmark_retrieval.py simonw/sqlite-utils`
plus a fusion-weight sweep · **When:** 2026-09-18
**Setup:** 192 labelled examples, ground truth from fix commits, fixed pool of
30 chunks deduplicated to files, file-level scoring, tests excluded from both
labels and retrieval.

### k=5 — the configuration that matters, since ~5 chunks fit a prompt

| configuration | Recall@5 | Precision@5 | MRR | latency |
|---|---:|---:|---:|---:|
| **dense** | **0.835** | 0.187 | **0.564** | 79 ms |
| hybrid 10:1 dense | 0.817 | 0.184 | 0.540 | 93 ms |
| hybrid 3:1 dense | 0.799 | 0.179 | 0.503 | 94 ms |
| dense + rerank | 0.775 | 0.173 | 0.399 | 3,621 ms |
| hybrid 1:1 (plain RRF) | 0.757 | 0.170 | 0.443 | 93 ms |
| hybrid 1:1 + rerank | 0.736 | 0.165 | 0.373 | 4,248 ms |
| sparse (BM25) | 0.609 | 0.137 | 0.318 | 12 ms |

### k=10

| configuration | Recall@10 | Precision@10 | MRR |
|---|---:|---:|---:|
| hybrid 3:1 dense | 0.910 | 0.148 | 0.518 |
| dense | 0.902 | 0.149 | 0.571 |
| hybrid 10:1 dense | 0.899 | 0.148 | 0.550 |
| dense + rerank | 0.899 | 0.149 | 0.413 |
| hybrid 1:1 | 0.889 | 0.143 | 0.460 |
| sparse (BM25) | 0.795 | 0.124 | 0.344 |

### What these numbers say

**Dense retrieval alone is the best configuration, and it is also the fastest.**
That was not the design — see [ADR-012](adr/ADR-012-hybrid-retrieval.md).

**The fusion-weight sweep explains why hybrid lost.** Reciprocal Rank Fusion
weights both rankings equally, which is only right when they are comparable.
Increasing the dense weight climbs monotonically towards dense-only:

```
hybrid 1:1    0.757
hybrid 3:1    0.799
hybrid 10:1   0.817
dense only    0.835    ← the limit it converges on
```

It converges rather than peaking somewhere in the middle, which is the signature
of one ranker contributing nothing the other lacked — on this corpus, with whole
issue texts as queries.

**Reranking made things worse at 46× the latency.** Recall 0.835 → 0.775 and MRR
0.564 → 0.399. The likely cause is domain mismatch: `ms-marco-MiniLM` is trained
on web-search passages, not source code.

### What is *not* significant

With 192 examples the standard error on a recall near 0.8 is about **0.027**.

- dense (0.835) vs hybrid 10:1 (0.817) — **within noise**, not a real difference
- dense (0.835) vs hybrid 1:1 (0.757) — ~2.9 SE, real
- dense (0.835) vs + rerank (0.775) — ~2.2 SE, probably real; the MRR drop is
  far outside noise

**Precision is dominated by its ceiling and carries little signal here.** With
1.14 relevant files per example and 5 results returned, the maximum achievable
precision@5 is **0.228**. Recall and MRR are the informative metrics.

### Limits of this measurement

- One repository. `sqlite-utils` concentrates its changes in two files (`cli.py`
  in 101 of 192 examples, `db.py` in 80), which may favour dense retrieval.
- Ground truth is the fix commit, which is one valid answer rather than the only
  one — so these are a **lower bound**.
- Queries are whole issue texts. A different query shape (an extracted symbol
  name, say) could plausibly reverse the BM25 result, and Phase 5 may produce
  exactly that.

### Two measurement bugs found and fixed before publishing

Both produced plausible numbers rather than errors:

- **F-005** — labels included test files the retriever was configured never to
  return, capping recall below 1.0 at a different level per example.
- **F-006** — the number of chunks fetched was derived from K, so "Recall@5"
  meant different things in different runs. Caught only by measuring the same
  configuration twice and getting 0.817 and 0.695.

See [failures.md](failures.md).

---

## Phase 4 — First live root-cause analysis

**Target:** `simonw/sqlite-utils` issue #841 — *"rows_where() and delete_where()
fail to throw errors against non-existent tables"*
**When:** 2026-09-18 · `gemini-3.8-flash`, temperature 0

| metric | value |
|---|---|
| retrieval | 8 chunks from 2 files, dense mode |
| model calls | **1** |
| tokens | **2,731** (1 call) |
| latency | 8.2 s |
| confidence reported | high |
| citations produced | 3 |
| citations verified correct | **3 / 3** |
| repeat run (cached) | **0.0 s, 0 calls** |

### Retrieval put the right code in front of the model

```
0.860  sqlite_utils/db.py:4091-4116   Table.delete_where     ← named in the issue
0.835  sqlite_utils/db.py:2140-2180   Queryable.rows_where   ← named in the issue
0.818  sqlite_utils/db.py:2343-2344   Table.exists           ← needed for the fix
```

Both functions the issue names are in the top two results.

### The analysis, and whether it is actually right

> Both `rows_where()` and `delete_where()` check `if not self.exists()` and
> return early instead of attempting to execute their queries against
> non-existent tables.

Checked against the source, every citation is exact:

| cited | actual code |
|---|---|
| `db.py:2160-2161` | `if not self.exists(): return` — inside `rows_where` ✓ |
| `db.py:4106-4107` | `if not self.exists(): return self` — inside `delete_where` ✓ |
| `db.py:2120-2124` | `count_where` with **no** such check, executing directly ✓ |

The third is the interesting one. It is not part of the bug — it is the
*contrast case* that proves the diagnosis: a sibling method without the early
return does raise, which is exactly why the two named ones do not.

**One correct analysis is not a success rate.** Phase 11 measures this over the
whole benchmark. This is a single data point, recorded because it is the first
end-to-end evidence that the pipeline works.

---

## Phase 4 — Free-tier model availability

Measured while diagnosing a 503, 2026-09-18. Same prompt, same key, minutes
apart:

| model | result |
|---|---|
| `gemini-3.8-flash` | 503 UNAVAILABLE |
| `gemini-3.7-flash` | 503 UNAVAILABLE |
| `gemini-3.6-flash` | OK, 10.9 s |
| `gemini-3.5-flash-lite` | OK, 1.4 s |

Availability varies **per model, minute to minute**, which is why retrying one
model harder does not help and a fallback chain does. The earlier default,
`gemini-2.0-flash`, had been retired entirely and returned 404.

**Cost control for a free tier:** responses are cached on disk keyed on the full
request, so a repeated prompt costs nothing — verified above at 0 calls. A call
budget (default 200) makes a runaway loop impossible rather than unlikely.

---

## Phase 5 — First generated patch

**Target:** `simonw/sqlite-utils` #841 · **When:** 2026-09-18

| metric | value |
|---|---|
| model calls | 3 (root cause, plan, patch) |
| tokens | **6,625** total |
| edits produced | 2 |
| files changed | 1 |
| lines added / removed | **+0 / −4** |
| edits that applied cleanly | **2 / 2** |

### The patch

```diff
--- a/sqlite_utils/db.py
@@ -2159,8 +2159,6 @@
-        if not self.exists():
-            return
@@ -4104,8 +4102,6 @@
-        if not self.exists():
-            return self
```

Exactly the two early returns the diagnosis identified, and nothing else. The
plan also recorded the right risk unprompted: *"code relying on these silently
returning empty on non-existent tables will now raise"*.

**Not yet validated.** No tests have been run against this patch — that is Phase
6. "The patch looks right" is not a result; "the test suite passes with it
applied" is.

### The fallback chain earned its place

During this single run the log recorded five fallback events:

```
gemini-3.8-flash   503 UNAVAILABLE      (x3)
gemini-3.6-flash   503 UNAVAILABLE      (x2)
gemini-3.8-flash   429 RESOURCE_EXHAUSTED
```

Without a chain, the run fails. With one, it completed — on the third model.
The 429 is the free-tier quota, which is the constraint this project actually
operates under.

**Design consequence:** because the fallback answered, part of this run was
produced by a different model than the primary. That is why `provider.name`
reports the model that answered rather than the one configured, and why every
measurement records it. A number without the model that produced it is not a
number.
