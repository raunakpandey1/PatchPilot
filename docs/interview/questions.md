# Question bank

Questions an interviewer would actually ask about this project, grouped by
topic, with answers grounded in code that exists in this repository.

Answers are written the way you would say them out loud: a claim, a reason, and
where possible a number.

> Sections are added as each phase lands. RAG, sandboxing, guardrails and
> evaluation arrive in Phases 3–11.

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


---

## Agents and orchestration

**What is an agent, as opposed to a single model call?**

A loop where a model decides an action, real code executes it, and the actual
result feeds back into the next decision. The defining feature is feedback from
a real environment. In PatchPilot the environment is a git repository and a test
runner, so the agent finds out whether its patch works rather than predicting
it. A single call would produce a plausible patch having never run anything.

**How much autonomy does yours have, and why?**

Deliberately little. The sequence of steps is fixed in the graph; the model is
used at specific points for specific judgements, each returning a declared
schema. Three reasons. It is auditable — a security review can be told exactly
which node is capable of touching git, which is unanswerable if routing emerges
from a conversation. It is debuggable, because a failed run can be compared
against a known path. And it is cheaper, because deciding what to do next is
itself a model call I am not making. The cost is that it cannot improvise a step
I did not anticipate, which for something editing other people's repositories is
the trade I want.

**Why LangGraph rather than a `while` loop?**

Three properties of this project. The debug loop is a bounded cycle, and as a
conditional edge the routing decision stays a small pure function you can test
with a dictionary, rather than tangling with the work inside a loop body. Human
approval spans process boundaries — the person answers hours later, possibly
from a second CLI run — so the whole state must survive to disk and resume,
which is a durable execution engine if you write it yourself. And re-running is
expensive: a crash at the sandbox step would repeat every model call, and on a
free tier that is quota I cannot get back. Checkpointing is a cost control here,
not an aesthetic.

**What does LangGraph cost you?**

An extra layer in every stack trace, an API that moves between versions, typing
friction that cost four documented ignores, and a state migration problem —
change the schema and old checkpoints no longer match, with nothing to migrate
them. In development I delete the checkpoint file; in production that is
genuinely unsolved.

**What is state, and why can it not be local variables?**

It is everything the run knows, as one serialisable value. Local variables die
when the function returns, and three things here require them to survive:
pausing for a human and resuming in a different process, recovering from a crash
without repeating expensive work, and looping with accumulated knowledge.

**What is a reducer and what breaks without one?**

A function that merges a node's output into existing state rather than replacing
it. By default, a node returning a key overwrites it — so if two nodes each
report an error, you keep only the second. Token usage is the clearest case:
without an accumulating reducer, the last node's count replaces all the others
and the cost of a run is unknowable. The rule is that if the answer to "what if
two nodes write this?" is "keep both", it needs a reducer.

**Why is the GitHub client not in the state?**

State is serialised to the checkpoint database, and an open socket does not
survive that. Live objects are passed in when the graph is built and closed over
by the nodes. A useful side effect: tests substitute an entire world — scripted
model, mock HTTP transport, temporary workspace — and the whole graph runs
offline.

**How does an agent pause for approval and resume later?**

The state is written to a checkpoint store after every step, keyed by a
`thread_id`. The run stops and the process can exit. When approval arrives, a
new invocation with the same `thread_id` loads that state and continues from the
next step. I verified it the strongest way available — one graph object runs the
agent, and a *different* graph object reads the completed state back out. If
anything were held in memory by the running graph, that test would fail.

**How would you scale the checkpointer?**

SQLite serialises writers, so several concurrent agents against one file will
contend. The switch point is concurrency. `PostgresSaver` implements the same
interface, so it is a change in one constructor rather than a rewrite — which is
most of why I did not hand-roll the storage.

---

## Working with models

**Why abstract the LLM provider? Is that not premature?**

It would be with one implementation; there are two in use today. The test suite
needs a model that is free, deterministic and offline, and no real provider is
any of those — so 121 tests run against a scripted implementation satisfying the
same protocol. The provider swap is a secondary benefit. My test for premature
abstraction is whether the second implementation exists now, not whether I can
imagine needing one.

**What actually breaks when you swap models?**

Prompts and comparability. The plumbing is a config change, but a prompt tuned
for one model is not automatically good on another, and benchmark numbers do not
transfer. That is why the provider name is recorded alongside every
measurement — a metric without the model that produced it is meaningless.

**How do you get reliable structured data out of a model?**

Declare a schema and use the provider's constrained-generation support, so the
model can only emit tokens that fit — rather than asking for JSON in the prompt
and parsing the reply. Then validate anyway, because constrained output can
still be truncated by the token limit. When validation fails I distinguish the
causes, because the fixes are opposite: truncation means raise
`max_output_tokens`, a genuine mismatch means the prompt is under-specified and
retrying identical input will fail identically.

**Your schema validated. Is the answer right?**

No — it has the right shape. `line: 40` is a valid integer whether or not the
bug is on line 40. Shape is the schema's job; correctness is the test suite's.
Conflating them is how you get a pipeline that is confidently wrong in a
well-formed way.

**Why rank issues with arithmetic instead of a model?**

Cost, stability, explainability — and stability is the one that decided it. An
LLM ranker orders differently every run, so when I improve retrieval in Phase 3
the agent would work on different issues and the benchmark comparison would be
meaningless. Determinism upstream is a precondition for measuring anything
downstream. It also ranks 77 issues in under 3 milliseconds for zero tokens, and
prints the factor table that answers "why was this skipped".

**Your ranking weights are made up. Is that not the same problem?**

They are guesses and I say so. The difference is that they are stable guesses I
can test: Phase 11 measures how well the `attempt` verdict predicts real fix
success, and tuning is a one-line change with a measurable effect. I already
have a hypothesis from real data — two issues scored `attempt` at over three
years stale, so the staleness weight is probably too low. A model's implicit
weights can be neither inspected nor tuned.

**A type checker rejected your working code. What did you do?**

Reduced it to the smallest file containing none of my own code. A
directly-defined node function passed; the same function returned from a factory
failed. That located it in LangGraph's overload inference rather than in my
types, so the four `type: ignore` comments are a documented decision with the
reduction recorded beside them. Silencing a checker without that evidence is how
a real bug ends up hidden behind a comment.

---

## RAG and retrieval

**What is RAG and why does this project need it?**

Retrieval-augmented generation: find the parts of your data relevant to a query
and put them in the prompt. Needed because the model has never seen this
repository and cannot be shown all of it — code tokenises densely, so a
500-line file can exceed 6,000 tokens, and filling a prompt with irrelevant
files measurably degrades answers rather than improving them. The engineering
problem is choosing what goes in.

**How do you chunk code, and why not the standard fixed-size approach?**

Parse it and cut on declaration boundaries — functions, classes, methods — using
Python's `ast`. Fixed-size chunking splits functions in half and both halves are
worse than useless: one has the signature without the logic, so it matches
searches it cannot answer, and the other has the logic without the name, so it
is unfindable. The information is present and retrieval can no longer reach it.
The standard advice is written for prose, where paragraphs are interchangeable;
code has explicit structure telling you where the seams are.

**What do you do with a class too big for one chunk?**

Split it into a signature-and-docstring summary plus one chunk per method, each
tagged with the class name. A thousand-line class would exceed the embedding
model's 512-token limit and be silently truncated, and a query about one method
would retrieve all of it. Bugs live in methods, so methods are the retrieval
unit.

**Why hybrid retrieval instead of vector search alone?**

They fail in opposite directions. Vector search handles paraphrase — "crashes
when the table doesn't exist" finds `raise NoTable` despite sharing no words —
and is weak on exact identifiers, because `rows_where` embeds near "things about
querying rows" rather than to the function of that name. BM25 is the reverse. A
GitHub issue contains prose *and* a traceback with exact symbols, so using one
method throws away half the query.

**How do you combine two rankings whose scores are not comparable?**

Reciprocal Rank Fusion. Cosine similarity is in [-1, 1]; BM25 is unbounded and
corpus-dependent, so adding them is meaningless and normalising them requires
knowing distributions that change per query. RRF discards the scores and sums
1/(k + rank) with k = 60, using only positions. Nothing needs normalising, and a
ranker producing wild scores cannot dominate — only its ordering counts.

**What is the difference between pre- and post-filtering, and why does it
matter?**

Post-filtering retrieves the nearest N and then discards the ones that fail the
filter — so if 45 of the top 50 are tests and you asked for 10 non-test results,
you get 5, with no error. Pre-filtering narrows to matching chunks first and
searches within them, returning the full 10. Since Recall@K is the number I
publish, a retrieval layer that quietly returns fewer results than requested
would make the metric wrong rather than just worse. I verified Qdrant
pre-filters with a constructed case rather than trusting documentation.

**What is the difference between a bi-encoder and a cross-encoder?**

A bi-encoder encodes query and document separately into vectors, so documents
can be embedded in advance and search is a fast index lookup — but the model
never sees the pair together. A cross-encoder takes the pair as one input, so
every layer can relate query tokens to document tokens; much more accurate, and
impossible to precompute. That trade is why the pipeline retrieves ~30
candidates cheaply and rescores them expensively.

**How do you evaluate retrieval without paying for labels?**

Git already recorded the answer. For each closed issue, the commit that closed
it names the files that had to change, so every closed issue is a labelled
example produced as a side effect of normal development. I report Recall@K,
Precision@K and MRR across four configurations, so the comparison rather than
any single number is the finding.

**Why is recall your headline metric?**

Because a file never retrieved can never be fixed — recall caps everything
downstream. Precision still matters, since irrelevant chunks consume context and
bury the relevant one, which is why both are reported at two values of K.
Otherwise you could maximise recall by returning everything.

**What is wrong with your benchmark?**

Several things, all making it conservative. The fix commit is one valid answer,
not the only one, so a genuinely relevant file that was not in that commit
scores as wrong — the numbers are a lower bound. Only issues closed by an
identifiable commit are usable, which may skew towards tidy fixes. Scoring is at
file granularity because that is what the labels support. And it is one
repository, so it is evidence rather than a law.

**Your benchmark had a ceiling below 1.0. How did you find that?**

By printing the ground truth before trusting it. The labels averaged two files
per example and about half were test files, because a fix commit touches the
source and its test together — while the retriever is configured to exclude
tests. So roughly half of every example's correct answers were unreachable by
construction, capping recall at a different level for each example. Nothing
failed; it would have printed a plausible number. The generalisable check is
that the labels and the system under test must agree on what a valid answer is.

**What breaks first at a thousand repositories?**

BM25. It is computed in-process, holding the corpus in memory and rebuilding per
repository — milliseconds for one repository, untenable for many large ones. The
fix is Qdrant's sparse-vector support so the inverted index lives in the
database. Second would be indexing throughput: 1,873 chunks took 504 seconds on
this CPU, which is fine once per repository and not fine a thousand times.

**Why did you not store everything in the vector database?**

Because vector search is approximate and ranked, which is right for "what looks
similar" and wrong for "which runs did this user approve" — a question with one
correct answer, needing transactions and foreign keys a vector store does not
have. The clearest version is lifecycle: the index is derived data I can delete
and rebuild, and the approval history is the record. One store makes it
ambiguous which is which, and that ambiguity is how someone eventually deletes
the wrong thing.
