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
