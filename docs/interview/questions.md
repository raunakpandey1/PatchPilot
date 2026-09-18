# Question bank

Questions an interviewer would actually ask about this project, grouped by
topic, with answers grounded in code that exists in this repository.

Answers are written the way you would say them out loud: a claim, a reason, and
where possible a number.

> Sections are added as each phase lands. Agents, RAG, LangGraph, sandboxing,
> guardrails and evaluation arrive in Phases 2–11.

---

## The project in general

**Explain PatchPilot in 60 seconds.**

It is an agent that tries to fix real GitHub issues. You give it a repository;
it clones the code, fetches the open issues, ranks which ones are actually
attemptable, retrieves the relevant parts of the codebase, works out a root
cause, writes a patch, runs the project's own test suite against that patch in a
sandbox, and loops on failure up to a limit. Nothing touches the repository
without a human approving it. It is built on LangGraph for orchestration,
hybrid retrieval over Qdrant for code search, and a Docker sandbox for
validation — and it is evaluated on a benchmark of real closed issues where the
commit that fixed each one gives the ground truth.

**Why build it?**

To learn how agents fail, not how they demo. The interesting engineering is not
the prompt — it is rate limits, retrieval quality, sandbox isolation, bounded
loops, and measuring whether any of it works.

**What is the honest state of it?**

*(Update as phases land.)* Phases 0 and 1 complete: GitHub client with
conditional-request caching, blobless cloning, and deterministic repository
analysis. 69 unit tests offline, 6 integration tests against the real API, mypy
strict clean.

---

## APIs, rate limits and reliability

**You have 5,000 requests an hour and 500 repositories to keep fresh. How?**

Do not fetch what has not changed. GitHub returns an ETag with every response;
send it back as `If-None-Match` and unchanged data comes back as a 304 that is
not charged against the budget. Then `since=` to request only issues updated
after the last sync, and poll active repositories more often than quiet ones.
Track the remaining budget from the response headers so you slow down before
being cut off. I measured it: a repeat fetch went from 2 requests to 0 budget
consumed.

**What is the difference between a primary and a secondary rate limit?**

The primary limit is the hourly budget; when it is gone, `x-ratelimit-remaining`
is 0 and retrying is pointless until the reset time. A secondary limit means you
are sending requests too quickly — the budget remains — and backing off then
retrying will work. Both can arrive as a 403, so the code branches on the header
rather than the status code. Getting it backwards means either hammering a door
that will not open for an hour, or abandoning a request that would have
succeeded in two seconds.

**What do you retry, and what do you never retry?**

Retry connection timeouts, 5xx, and secondary rate limits — failures where the
same request later gets a different answer. Never retry 404, 401, or an
exhausted primary limit: retrying cannot change the outcome and turns a clear
error into a slow one. Backoff is exponential with jitter; the jitter stops many
clients retrying in lockstep and recreating the overload they are backing off
from.

**Why not GraphQL, given the N+1 problem?**

GraphQL wins when you have many per-item follow-up fetches. We mostly read issue
lists, which REST returns 100 at a time, so N+1 barely applies. What is scarce
here is rate-limit budget, and REST's conditional requests take a repeat fetch
to zero cost — which does not carry over to GraphQL, because it is a single POST
endpoint that HTTP caching cannot key on. REST also has real status codes, which
makes the retry policy above expressible. If we later needed comments for many
issues at once, that is a genuine N+1 and I would add a GraphQL adapter behind
the same interface.

---

## Git

**Where do GitHub issues live?**

Not in the repository. In GitHub's database, reachable only over the API. Any
design that only clones can never see them — which is why this project talks to
two separate systems about the same project, and why they need different
caching and different failure handling.

**You clone with `--depth=1`. What did you lose?**

History. `git log` returns one commit and `git blame` cannot attribute a line to
the change that wrote it. Root-cause analysis depends on both, and the retrieval
evaluation depends on reading the commit that closed an issue. Measured on
flask: shallow reads 1 commit, blobless reads 200, at 2.6 MB versus 6.5 MB. The
4 MB buys back the capability.

**What is a blobless clone?**

`--filter=blob:none`. It downloads every commit and tree but no file contents up
front, fetching individual blobs on demand. You get the full commit graph — so
history operations work — and skip the bulk, which is old versions of files
nobody reads. Measured 39–55% smaller than a full clone, for 20–35% more
wall-clock time because the server computes a custom pack.

---

## Security

**`git clone` does not execute code. Name three ways cloning a hostile
repository can still hurt you.**

Submodules: `.gitmodules` is attacker-controlled text saying "also fetch from
this URL", so recursing means fetching wherever they point. Size: a repository
can be tens of gigabytes or contain millions of files, filling the disk or
hanging the clone. And credentials: git reads your global config by default,
including the credential helper, so a hostile URL can be handed your stored
token. A fourth is symlinks pointing outside the tree, which is why every
filesystem access goes through a confinement check.

**How do you stop `../../etc/passwd` escaping your workspace?**

Resolve the path to an absolute real path — following symlinks — then check it
is still under the root, with `Path.resolve()` and `is_relative_to()`. Never a
string-prefix check: `workspace/link/passwd` starts with the workspace path as a
string but can resolve to `/etc/passwd` if `link` is a symlink to `/etc`. There
is a test for exactly that case.

**Why is the test command a tuple rather than a string?**

A string has to be handed to a shell to be run, and a shell interprets `;`,
`&&`, `|` and backticks — in a value derived from an untrusted repository. An
argument list goes to the operating system unparsed. It costs nothing and
removes a whole class of command injection before the sandbox phase even exists.

**Your security control blocked your own tests. What did you do?**

Made the exception explicit rather than removing the control. Blocking git's
`file` transport also blocks cloning from a local fixture repository, so I added
an `allow_file_protocol` parameter defaulting to False, opted in at the test call
sites, and added a test asserting the default still refuses. A control that
never inconveniences anyone is usually not doing anything.

---

## Testing

**How do you test code that depends on a third-party API?**

Inject the boundary. The HTTP client takes a `transport` in its constructor, so
tests pass a mock transport returning canned responses; retries, caching,
pagination and parsing all execute for real, and only the socket is replaced.
That keeps 69 unit tests offline and under two seconds, so they get run on every
change. A separate six-test integration suite hits the real API to catch the one
thing mocks cannot — our model of the API being wrong.

**What are the limits of mocking?**

A mock returns what you told it to, including things the real service never
sends. Mine did exactly that: it returned a 304 *with* a `Link` header, and the
real GitHub omits it. That difference hid a bug where a warm cache silently
returned 21% fewer issues. Mocks verify your logic; only real calls verify your
assumptions.

**Your retry policy waits up to 30 seconds. How do you test it quickly?**

Make the backoff a constructor parameter and set it to zero in tests. Generally:
anything that sleeps, reads the clock, or uses randomness has to be injectable
or it cannot be tested in reasonable time.

**Why split unit and integration tests?**

Because a slow suite stops being run. The unit suite needs nothing and finishes
in two seconds, so it runs on every change — that is where the value is. The
integration suite is slow, needs a token and consumes rate limit, so it runs
deliberately. Both exist; they answer different questions.

---

## Design and judgement

**Why is there no LLM in Phase 1?**

Because every fact it needs is already written down. The test runner is declared
in `pyproject.toml`; the Python version is in `requires-python`. Reading a file
is free, instant and identical every time, while a model costs quota, varies
between runs, and can invent a command that does not exist. That last failure
propagates: a fabricated test command fails in the sandbox, the debug loop
spends several model calls fixing a patch that was never the problem, and the
benchmark records the wrong cause. The rule I applied throughout is **model for
judgement, code for lookup**.

**Why build the deterministic layer first?**

Everything above it is judged against it. RAG indexes this clone, the sandbox
runs this test command, the benchmark pins this commit. An error here surfaces
three phases later wearing a disguise, and by then it is wrapped in confident
prose from a language model. Probabilistic layers should sit on trustworthy
deterministic ones, not the other way round.

**What would you do differently?**

Write the comparison test earlier. I had tests for caching and tests for
pagination, both passing, while the interaction between them lost a fifth of the
data. When an optimisation is supposed to preserve behaviour, the test that
matters is the one asserting optimised and unoptimised results are identical —
and I only got there by measuring on real data.
