# Project interview story bank

STAR stories built only from things that actually happened while building
PatchPilot. Every number is from [metrics.md](../metrics.md); every bug is in
[failures.md](../failures.md).

**Situation → Task → Action → Result.** Keep the situation short. Most of the
value is in the action, and interviewers listen hardest to the result.

---

## S-001 — A caching optimisation that silently lost 21% of the data

*Answers:* "tell me about a difficult bug", "tell me about a time you found a
problem nobody else had noticed", "tell me about a time your tests let you
down".

**Situation.** PatchPilot runs on a free GitHub API budget — 5,000 requests an
hour — and needs to keep issue lists fresh across repositories. I implemented
conditional requests: store the ETag GitHub returns, send it back as
`If-None-Match`, and unchanged data comes back as a 304 that does not count
against the budget.

**Task.** Verify it actually worked, rather than assume it did. I wrote a
benchmark that fetched issues twice and reported the rate-limit budget consumed
by each run.

**Action.** The benchmark confirmed the saving — the second run cost zero
budget — but it also printed something I was not looking for: the cold run
returned 77 issues and the warm run returned 61. No error, no warning.

The warm run had made one fewer HTTP request than the cold run. That pointed at
pagination rather than parsing, because a parsing bug loses items inside a page
while a pagination bug loses a whole page. GitHub paginates with a `Link`
response header, so I checked the real API directly: a 200 response includes
`Link`; a 304 does not. My client read the next-page URL from the live response,
so as soon as the cache was warm the link was gone and pagination stopped after
page one.

The fix was to store the `Link` header in the cache alongside the body and
restore it when serving a 304. I then wrote a regression test using a mock
transport that omits `Link` from its 304 exactly as GitHub does.

**Result.** Both runs return 77 issues, and the warm run still costs zero
rate-limit budget. The regression test is
`test_pagination_survives_a_warm_cache`.

**What I took from it.** I had tests for caching and tests for pagination, and
both passed — the bug lived in their interaction. Since then, when an
optimisation is meant to preserve behaviour, I write the test that compares the
optimised and unoptimised results against each other, rather than testing each
path in isolation. And the bug was found by measuring, not by testing, which is
why the benchmark reports what it returned and not only how fast it was.

---

## S-002 — Choosing a clone strategy by measurement rather than convention

*Answers:* "tell me about a data-driven decision", "tell me about a tradeoff",
"tell me about a time you questioned the standard approach".

**Situation.** PatchPilot clones repositories to analyse them, on a laptop with
8 GB of RAM. The conventional choice for tooling is `git clone --depth=1`,
because it is the fastest and smallest.

**Task.** Pick a strategy that would still work for later phases, which need
`git log` and `git blame` for root-cause analysis, and need to read the commit
that closed an issue to build a retrieval evaluation set.

**Action.** Rather than reason about it, I wrote a benchmark comparing full,
shallow and blobless clones across three real repositories, measuring wall-clock
time, disk size, and — the part that mattered — how many commits of history
remained readable afterwards.

**Result.** Shallow was smallest and fastest and returned **one** commit of
history, which would have made root-cause analysis impossible. Blobless
(`--filter=blob:none`) kept full history at 45% of a full clone's size on
`pallets/flask` — 6.5 MB against 14.5 MB — for 20–35% more wall-clock time. I
chose blobless and recorded both the numbers and the time cost in an ADR.

One run showed blobless taking 3.91 s against a typical 1.4 s. I re-ran it three
times, got 1.41–1.47 s, concluded the first was an outlier, and recorded it in
the metrics file rather than deleting it.

**What I took from it.** The cheapest option was the one that could not do the
job, and I would not have known that without measuring a *capability* rather
than only cost. I now try to include at least one correctness or capability
column in any performance comparison.

---

## S-003 — A security control that blocked my own tests

*Answers:* "tell me about a time you had to balance security and developer
experience", "tell me about a time you pushed back on the easy fix".

**Situation.** Cloning runs with `-c protocol.file.allow=never`, which blocks
git's `file` transport. Submodules can use that transport to read the local
filesystem, and a `.gitmodules` file is written by whoever controls the
repository being cloned.

**Task.** Ten git tests failed at once with `fatal: transport 'file' not
allowed`. The tests clone from a fixture repository on local disk — which is
exactly the transport being blocked.

**Action.** The quick fix was to drop the flag. I did not, because the control
was doing precisely what it was added for. Instead I added an explicit
`allow_file_protocol=False` parameter: tests opt in at the call site, production
never does. I also added a test asserting the default refuses a local clone, so
the control itself is now covered.

**Result.** Tests pass, the control is intact, and the one legitimate exception
is visible at each call site rather than hidden in a weakened default.

**What I took from it.** When a security control gets in your way, the question
is "is this particular use legitimate, and can I make it explicit?" — not "how
do I turn it off?". A control that never inconveniences anyone usually is not
doing anything.

---

## S-004 — Choosing not to use an LLM in an AI project

*Answers:* "tell me about a time you avoided a technology", "tell me about a
decision that prevented a future problem".

**Situation.** PatchPilot needs to know each repository's test command, package
manager and language. Building an AI agent, the obvious approach was to give a
model the config files and ask.

**Task.** Decide how this component should work, knowing that four later phases
consume its output.

**Action.** I chose deterministic parsing. Every one of these facts is already
declared in `pyproject.toml`, `tox.ini` or the CI config, so reading the file is
free, instant and identical every time. A model would cost quota I do not have
on a free tier, vary between runs, and occasionally invent a command that does
not exist.

The last failure is the one that decided it. A fabricated test command fails in
the Phase 6 sandbox, the Phase 7 debug loop then spends several model calls
trying to fix a patch that was never the problem, and the Phase 11 benchmark
records a failure with entirely the wrong cause. A wrong answer in a
deterministic layer does not stay there — it gets laundered into confident prose
downstream.

**Result.** Fifteen tests covering the analyzer run offline in milliseconds,
including adversarial cases like `src/latest/thing.py` not being mistaken for a
test file. When the rules do not match a project it reports `UNKNOWN` and the
agent declines work it cannot validate, rather than guessing.

**What I took from it.** A rule I applied through the rest of the project: **use
the model for judgement, use code for lookup.** "What test runner is this" is
lookup. "Is this issue worth attempting" is judgement.

---

## Stories not yet written

Placeholders, so the gaps stay honest. These will be filled only if the
corresponding thing actually happens.

- Retrieval quality — a measured improvement from hybrid search or reranking (Phase 3)
- A debug loop that failed to converge, and what fixed it (Phase 7)
- A prompt injection that got through, and the defence added (Phase 8)
- A cost or latency optimisation with before/after numbers (Phase 10)
