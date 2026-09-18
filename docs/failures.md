# Failure log

Real bugs from building PatchPilot. Nothing here is invented — each entry is a
thing that actually broke, with the symptom as it was first seen.

This file exists because interviews ask *"tell me about something that went
wrong"* far more often than *"tell me what worked"*, and because a bug you
cannot describe is a bug you did not really understand.

---

## F-001 — An empty cache silently disabled itself

| | |
|---|---|
| **Date** | 2026-09-17 |
| **Phase** | 1 |
| **Component** | `tools/github.py`, `tools/http_cache.py` |
| **Severity** | Would have cost ~100% of the rate-limit savings |

**Symptom.** Two cache tests failed. The client behaved as though no cache had
been passed at all: no `If-None-Match` header was ever sent, and after a
successful request the cache was still empty.

**Investigation.** The caching code looked correct, so the question became
whether it ran at all. A three-line script printed `len(cache)` after one
request: zero. So `cache.set()` was never reached — or the cache being written
to was not the cache being inspected.

**Root cause.** The constructor read:

```python
self.cache = cache or NullCache()
```

`MemoryCache` defines `__len__`. In Python, an object with `__len__` returning
`0` is **falsy**. So a freshly-created empty cache — exactly what every caller
passes — evaluated as false, and `or` quietly replaced it with a cache that
does nothing.

**Fix.** Test for the thing actually being asked about:

```python
self.cache = cache if cache is not None else NullCache()
```

**Verification.** The two failing tests pass, and
`test_etag_is_sent_on_the_second_request_and_304_serves_cache` asserts the
`If-None-Match` header is present on the second request.

**Prevention.** Never use `or` for a default when the value can be a container
or any object defining `__len__` or `__bool__`. `x if x is not None else y` says
what is meant.

**Lesson.** The dangerous bugs are the ones where nothing errors. This version
worked perfectly — it just used 5,000 requests an hour instead of almost none,
and would only have been noticed as an unexplained rate-limit exhaustion weeks
later.

---

## F-002 — A warm cache silently truncated paginated results

| | |
|---|---|
| **Date** | 2026-09-17 |
| **Phase** | 1 |
| **Component** | `tools/github.py`, `tools/http_cache.py` |
| **Severity** | Silent data loss — ~21% of issues missing |

**Symptom.** Found by measurement, not by a test. Running
`scripts/benchmark_github.py` printed:

```
run           issues  seconds  requests   304s  budget spent
cold cache        77     1.19         3      0             2
warm cache        61     0.69         2      2             0
```

The rate-limit saving worked exactly as intended — and the warm run returned
**61 issues instead of 77**. No error, no warning.

**Investigation.** The warm run made one fewer HTTP request than the cold run,
which pointed at pagination rather than at parsing. GitHub returns at most 100
items per page and puts the next page's URL in a `Link` response header. A
direct check against the real API:

```
200 response:  link present: True
304 response:  link present: False
```

**Root cause.** GitHub does not send the `Link` header on a `304 Not Modified`.
The client read the next-page URL from the live response, so with a warm cache
there was no link, `_paginate` concluded there were no more pages, and stopped
after page one. The first page's 100 raw items became 61 issues after pull
requests were filtered out.

**Fix.** Store the `Link` header in the cache next to the payload
(`CachedResponse.link`) and restore it when serving a 304.

**Verification.** Re-running the benchmark gives 77 issues on both runs, still
at zero budget for the warm run. A unit test,
`test_pagination_survives_a_warm_cache`, reproduces the exact conditions with a
mock transport that omits `Link` from its 304, exactly as GitHub does.

**Prevention.** When caching an HTTP response, ask what else the response
carried besides the body. Headers are part of the answer, not decoration.

**Lesson.** This bug was invisible to the unit tests because they tested cold
and warm behaviour separately, and each passed. It only appeared when the two
were compared *against each other* on real data. An optimisation is not
finished when it is fast — it is finished when it has been proven to return the
same answer.

---

## F-003 — Our own security control blocked our own tests

| | |
|---|---|
| **Date** | 2026-09-17 |
| **Phase** | 1 |
| **Component** | `tools/git.py` |
| **Severity** | None in production; a test-design problem |

**Symptom.** Ten git tests failed at once with
`fatal: transport 'file' not allowed`.

**Root cause.** Not a bug. `clone()` passes
`-c protocol.file.allow=never` to block git's `file` transport, which
submodules can use to read the local filesystem. The tests clone from a
fixture repository on local disk, which uses exactly that transport.

**Fix.** Added an explicit `allow_file_protocol: bool = False` parameter. Tests
opt in at the call site; production never does. The alternative — weakening the
default — would have removed a real control to make a test convenient.

**Verification.** `test_file_protocol_is_blocked_by_default` now asserts the
default refuses a local clone.

**Lesson.** A security control that never inconveniences anyone is usually not
doing anything. When one blocks you, the question is "is this use legitimate,
and can I make it explicit?" — not "how do I turn it off?"

---

## F-004 — `mypy --strict` rejected valid LangGraph code

| | |
|---|---|
| **Date** | 2026-09-18 |
| **Phase** | 2 |
| **Component** | `agent/graph.py` |
| **Severity** | None at runtime — a typing-only failure |

**Symptom.** Four `mypy` errors, one per node:

```
error: No overload variant of "add_node" of "StateGraph" matches argument
types "str", "Callable[[AgentState], AgentState]"  [call-overload]
```

The tests passed. The graph ran correctly against the real repository. Only the
type checker objected.

**Investigation.** The tempting move is to add `# type: ignore` and continue.
That is fine *if* you know whose bug it is — and at this point I did not. A
silenced error hiding a real mistake is worse than the error.

So: reduce it to the smallest thing that reproduces, with none of my own code in
it. Six lines:

```python
class S(TypedDict, total=False):
    x: int

def node_a(state: S) -> S:        # directly-defined function
    return S(x=1)

StateGraph(S).add_node("a", node_a)          # ✅ mypy --strict: Success
```

Passes. Now the only change that matters — the node comes from a factory:

```python
NodeFn = Callable[[S], S]

def make() -> NodeFn:
    def node(state: S) -> S:
        return S(x=1)
    return node

StateGraph(S).add_node("a", make())          # ❌ same error
```

Fails. Same types, same function body; the only difference is whether mypy sees
a `def` or a value whose type is a `Callable` alias.

**Root cause.** LangGraph's `add_node` overloads infer their node type parameter
from a directly-defined function. Mypy cannot infer it through a `Callable[...]`
alias, which is what a node *factory* returns. Our nodes come from factories
because they close over dependencies — see
[deps.py](../src/patchpilot/agent/deps.py) — so every one of them hits this.

**Fix.** Four `# type: ignore[call-overload]` comments, each with the reduction
above recorded in the code next to them, so a future reader knows it was
diagnosed rather than silenced.

**Verification.** `mypy --strict` clean across 24 files; 121 tests pass; the
graph runs correctly against the real API.

**Prevention.** Not preventable — it is upstream. What is repeatable is the
method: **before silencing a type error, reduce it to a file containing none of
your own code.** If the minimal case still fails, it is theirs. If it passes, it
is yours, and you just found it.

**Lesson.** `# type: ignore` is a claim that you know better than the checker.
Making that claim without evidence is how a real bug gets hidden behind a
comment. Five minutes of reduction turned a guess into a fact — and into a
documented one.

---

## F-005 — The benchmark could not have scored 1.0, by construction

| | |
|---|---|
| **Date** | 2026-09-18 |
| **Phase** | 3 |
| **Component** | `evaluation/retrieval.py` |
| **Severity** | Every retrieval number would have been wrong — biased low, by a different amount per example |

**Symptom.** None. Nothing failed. The benchmark ran for 24 minutes and would
have printed a table of plausible-looking numbers.

I found it by printing the ground truth before trusting it:

```
avg relevant files per example: 1.96
  #50: "Too many SQL variables" on large inserts
      -> sqlite_utils/db.py, tests/test_create.py
  #77: Ability to insert data transformed by a SQL function
      -> sqlite_utils/db.py, tests/test_conversions.py
```

Two files per example, and roughly half of them are **test files**.

**Investigation.** The evaluator retrieves with `exclude_tests=True` — correctly,
because the question retrieval answers is "where is the bug", and the bug is in
production code.

But the ground truth was built from *every* `.py` file a fix commit touched, and
a fix commit almost always touches the source file and its test together.

So for an example labelled `{sqlite_utils/db.py, tests/test_create.py}`, the
retriever is configured never to return the second one. Maximum achievable
recall for that example is **0.5**. Across the set, the ceiling sat somewhere
below 1.0 — and at a *different* level for every example, depending on how many
of its files happened to be tests.

**Root cause.** The labels and the retriever disagreed about what counts as a
valid answer. Ground truth said "these files changed"; the retriever was told
"never return tests". Neither is wrong alone; together they made the metric
measure something other than retrieval quality.

**Fix.** `build_ground_truth(exclude_test_files=True)` — filter test files from
the labels so they match what retrieval is permitted to return. The *size* check
still runs on the full change, because a commit touching twelve files is a
refactor whether or not most of them were tests.

The exclusion is a parameter rather than a hard-coded assumption, so the effect
of the choice can itself be measured later.

**Verification.** Two tests: one asserting a fix commit touching
`src/thing.py` and `tests/test_thing.py` yields only the source file, and one
asserting the old behaviour is still reachable for comparison.

**Prevention.** Print the ground truth before running anything against it. Three
examples were enough.

**Lesson.** This is the failure mode that makes benchmarks dangerous rather than
merely useless: it produces a number. A crash tells you something is wrong. A
biased metric tells you 0.62 and lets you spend a week optimising against a
ceiling you did not know was there — and every comparison against it inherits
the bias silently.

The generalisable check: **the labels and the system under test must agree on
what a valid answer is.** If the system is forbidden from producing an answer
your labels call correct, you are measuring the restriction, not the system.

A second, smaller finding came out of the same investigation. The first run took
24 minutes, dominated by cross-encoder reranking — 200 examples × 30 candidates
× 2 values of K = 12,000 scorings on CPU. Since K only slices an existing ranked
list, one retrieval pass can be scored at every K, halving the work. That is
`evaluate_at_multiple_k`.

---

## F-006 — Configurations were not comparable, because the fetch size depended on K

| | |
|---|---|
| **Date** | 2026-09-18 |
| **Phase** | 3 |
| **Component** | `evaluation/retrieval.py` |
| **Severity** | Cross-configuration comparisons were invalid — the thing the benchmark exists to do |

**Symptom.** Two runs disagreed about the same configuration. The main benchmark
reported dense retrieval at **Recall@5 = 0.817**. A follow-up experiment,
sweeping fusion weights, reported the identical dense configuration at
**0.695** on the identical data.

Same code, same index, same 192 examples, a 0.12 difference. One of the two
numbers was wrong, and I did not know which.

**Investigation.** The only difference between the runs was the set of K values
requested: the main benchmark asked for `ks=(5, 10)`, the sweep for `ks=(5,)`.

That should not matter. K decides how many results to *score*, not how many to
*fetch*. Except the code read:

```python
fetch = candidate_pool if reranker else max_k
```

Without a reranker, the number of chunks retrieved was `max(ks)` — 10 in one
run, 5 in the other.

Which matters because **scoring is at file granularity while retrieval returns
chunks**, and several chunks routinely come from the same file. `sqlite-utils`
is a repository where 101 of 192 labelled examples point at `cli.py` and 80 at
`db.py`, so chunks cluster heavily. Fetching 5 chunks might yield only 2 or 3
distinct files; fetching 10 might yield 6.

So "Recall@5" was not measuring the top 5 files. It was measuring *however many
files happened to fall out of max(ks) chunks* — a different quantity per run.

**Root cause.** A hidden coupling between how many results are scored and how
many are retrieved. Each is reasonable alone; together they made the headline
metric depend on an unrelated parameter.

**Fix.** Always retrieve a fixed candidate pool (30 chunks), deduplicate to
files, then score the top K files. Every configuration now sees the same pool,
so "Recall@5" means the same thing everywhere.

**Verification.** A regression test injects a recording retriever and asserts
the requested limit is the pool size regardless of the K values —
`test_the_candidate_pool_does_not_depend_on_k`.

**Prevention.** When a metric is written `Recall@K`, check what K actually
controls in the code. Here it silently controlled two things.

**Lesson.** This one was only visible because **the same configuration was
measured twice in different contexts**. A single run would have produced 0.817,
looked plausible, and gone into the documentation.

Two separate measurement bugs in one phase — F-005 and this one — and neither
produced an error or a failing test. Both produced *numbers*. The practice that
caught both was refusing to believe the first result: print the labels before
trusting them, and re-measure the same thing a second way before publishing it.
