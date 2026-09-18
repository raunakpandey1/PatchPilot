# Testing code that depends on someone else's API

## In one sentence

Never let your test suite touch the real internet, because a suite that is slow
and flaky is a suite nobody runs.

## The problem it solves

`GitHubClient` is useless without GitHub. So how do you test it?

The obvious answer — call the real API — fails on five counts:

1. **Slow.** Every test waits on a network round trip.
2. **Flaky.** GitHub has a bad minute; your build goes red for no reason.
3. **Rate-limited.** Your test suite competes with your actual work for 5,000
   requests an hour.
4. **Needs a secret.** Nobody can run your tests without provisioning a token.
5. **Not reproducible.** The data changes. A test asserting "77 open issues"
   breaks when someone files one.

A suite with those properties gets run before commits at best, and eventually
not at all.

## How it works, step by step

The trick is a **seam** — a place where the real thing can be swapped for a
fake one without the code under test noticing.

In PatchPilot, that seam is one constructor parameter:

```python
def __init__(self, token=None, *, transport: httpx.BaseTransport | None = None):
    self._client = httpx.Client(..., transport=transport)
```

`httpx` sends every request through a *transport*. Normally that transport opens
a socket. In tests we hand it one that does not:

```python
def handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"full_name": "simonw/sqlite-utils", ...})

client = GitHubClient(token="fake", transport=httpx.MockTransport(handler))
```

The client cannot tell the difference. Every layer above — retries, caching,
pagination, parsing — runs for real. Only the socket is replaced.

That is the whole technique, and it is why the constructor takes a `transport`
at all.

### What this makes testable that otherwise is not

Because `handler` is just a function, you can express any scenario — including
ones that are hard or impossible to trigger against the real API:

```python
def handler(request):
    if request.headers.get("if-none-match") == '"abc123"':
        return httpx.Response(304, headers=rate_limit_headers())
    return httpx.Response(200, json=REPO_JSON, headers={"etag": '"abc123"'})
```

Three assertions come out of that: the ETag was stored, `If-None-Match` was sent
on the second call, and the cached body was served. Against the real API you
could not force a 304 on demand.

Similarly:

- **Rate limit exhausted** — return `403` with `x-ratelimit-remaining: 0` and
  assert we do *not* retry.
- **Server error** — return `503` and count how many attempts are made.
- **Network blip** — `raise httpx.ConnectTimeout(...)` and assert recovery.

None of those are reachable on demand against a live service.

### The other half: retries must not really sleep

A retry policy with exponential backoff waits 1s, then 2s, then 4s. A test of
that policy would take seven seconds — which is how test suites become slow.

So the policy is configurable rather than hard-coded:

```python
GitHubClient(max_attempts=2, backoff_initial_s=0.0, backoff_max_s=0.0)
```

This is a general rule worth internalising: **anything that sleeps, reads the
clock, or generates randomness should be injectable**, or it cannot be tested
quickly.

## In PatchPilot

- [`tests/unit/test_github.py`](../../tests/unit/test_github.py) — 15 tests,
  no network, runs in under a second.
- [`tests/integration/test_github_live.py`](../../tests/integration/test_github_live.py)
  — 6 tests against the real API, marked `integration`.
- [`tests/unit/test_git.py`](../../tests/unit/test_git.py) — builds a real git
  repository in a temp directory rather than mocking git, because the thing
  worth testing is our parsing of git's real output.

## The split, and why both halves exist

```
poetry run pytest -m "not integration"   # 69 tests, ~2s, offline — run constantly
poetry run pytest -m integration          # 6 tests, real GitHub — run deliberately
```

Unit tests prove **our logic is right**. They cannot prove our *idea of the API*
is right — a mock returns whatever you told it to, including things GitHub would
never send.

That is what the integration tests are for. They are few, slow, and run on
purpose. They caught nothing here, but they are the only thing that would catch
GitHub changing a response shape.

**A note on mocking git.** `test_git.py` does not mock git. It creates a real
repository with real commits, because what needs testing is whether we parse
git's actual output — including a commit subject containing commas, pipes,
quotes and backslashes. A mock would only prove we can parse strings we invented
ourselves.

## What goes wrong

**Mocking too deep.** If you mock `GitHubClient.list_issues` itself, you have
tested nothing — you asserted that your mock returns what you told it to.
Replace the outermost boundary you own (the transport), not your own logic.

**Mocks that drift from reality.** Our mock originally returned a 304 *with* a
`Link` header. The real GitHub does not. That difference hid a real bug — see
[failures.md](../failures.md) F-002. When you learn a fact about the real API,
push it into the mock.

**Testing cold and warm separately.** Both of our cache tests passed while the
warm path silently returned 21% less data. The bug only appeared when cold and
warm results were compared *to each other*. If an optimisation is supposed to
preserve behaviour, write the test that asserts exactly that.

## Interview questions

**Q: How do you test code that depends on a third-party API?**

Inject the boundary. Our HTTP client takes a `transport` in its constructor, so
tests pass a mock transport returning canned responses; everything above the
socket — retries, caching, pagination, parsing — executes for real. That keeps
the unit suite offline and fast, so it gets run. A small, separately-marked
integration suite hits the real API to catch the one thing mocks cannot: our
model of the API being wrong.

**Q: What are the limits of that approach?**

A mock returns what you told it to, which can be something the real service
never sends. Ours did exactly that and hid a pagination bug for a while. Mocks
verify your logic; only real calls verify your assumptions. You need both, in
different suites, run at different frequencies.

**Q: Your retry logic waits up to 30 seconds. How do you test it without
waiting?**

Make the backoff a constructor parameter and set it to zero in tests. More
generally, anything that sleeps, reads the clock, or uses randomness has to be
injectable or it cannot be tested quickly.

**Q: Why not just mock the whole client?**

Then the tests assert that your mock does what you configured it to do. The
bugs in this phase — a falsy empty cache, a missing `Link` header on 304 — all
lived in the layers between the public method and the socket. Mocking the
client removes exactly the code that was broken.

## Official documentation

- [pytest — marking test functions](https://docs.pytest.org/en/stable/how-to/mark.html) — how the `integration` marker works.
- [pytest — fixtures](https://docs.pytest.org/en/stable/how-to/fixtures.html) — `tmp_path`, `monkeypatch`, and scoping.
- [HTTPX — mock transports](https://www.python-httpx.org/advanced/transports/#mock-transports) — the seam used here.
