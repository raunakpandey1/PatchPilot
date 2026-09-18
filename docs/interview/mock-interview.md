# Mock interview

Six levels, from "explain it in a minute" to "tell me about a failure". Every
answer below is grounded in something that actually happened in this repository
— there is nothing here you would have to bluff.

**How to use this:** cover the answers, say yours out loud, then compare. The
gap between what you said and what is written is what to work on. Answers are
written the way you would speak them, not the way you would write them.

A note on tone: the strongest thing about this project is that it is honest
about its limits. Interviewers notice that, and it is far more persuasive than a
polished success story. Where an answer says "I do not know", keep that.

---

## Level 1 — Explain it in 60 seconds

> **Tell me about PatchPilot.**

It is an agent that attempts real GitHub issues. You point it at a repository;
it ranks the open issues by how attemptable they look, retrieves the relevant
code, diagnoses a root cause with citations, writes a minimal patch, runs the
project's own test suite against it inside a locked-down container, and repairs
the patch when tests fail — up to a bound. Nothing reaches the repository
without a human approving a diff.

It is built on LangGraph for orchestration, local embeddings with Qdrant for
retrieval, and Docker for the sandbox. The interesting part is not the prompting
— it is the measurement. Retrieval is benchmarked against ground truth taken from
git history, and the answer contradicted my design: dense retrieval beat hybrid,
and reranking made things worse.

*What makes this answer work:* it names the pipeline, names the stack once, and
lands on a concrete finding. It does not claim a success rate, because there
isn't one yet.

---

> **Why did you build it?**

To learn how agents fail rather than how they demo. Almost every agent project
you see is a screen recording of one good run. I wanted the version where you
measure it, and the interesting engineering turned out to be rate limits,
retrieval quality, sandbox isolation, bounded loops and honest metrics — not the
prompts.

---

## Level 2 — Walk me through the architecture

> **How does a run work, end to end?**

Thirteen nodes in a LangGraph state machine.

Deterministic first: analyse the repository — languages, package manager, test
command, all read from config files rather than asked of a model. Then fetch and
rank the open issues, which is arithmetic over six weighted factors with hard
blockers, so the same input always produces the same order.

Then the model gets involved. Retrieve about eight chunks of code for the chosen
issue. Diagnose a root cause, returned as a schema with required file-and-line
citations. Plan a fix. Generate the patch as exact-text replacements.

Then deterministic again: apply to an isolated copy, run the tests in a
container with no network, and either pass, repair, or give up. Finally the
policy engine checks the patch, and a human approves.

*The thing to emphasise:* the model is used at four specific points for
judgement. It never decides what to do next.

---

> **Why fix the control flow instead of letting the model choose tools?**

Three reasons, and I would give the auditability one first because it is the
strongest. A security review has to be able to state exactly which node can
touch git — with free tool choice, the honest answer is "any of them,
potentially". Second, a failed run can be compared against a known path. Third,
deciding what to do next is itself a model call, and not making it thousands of
times is free, which on a free tier is the difference between running the
benchmark and not.

The cost is real: it cannot improvise a step I did not anticipate. For something
that edits other people's repositories, that is the trade I want. For an
open-ended research assistant it would be the wrong way round.

---

## Level 3 — Deep technical

> **Why LangGraph and not a while loop?**

Three properties of this project, none of which is "it's popular".

The debug loop is a bounded cycle, and as a conditional edge the routing
decision stays a small pure function I can test with a dictionary, instead of
tangling with the work inside a loop body.

Human approval spans process boundaries — the person answers hours later,
possibly from a second CLI invocation — so the whole state has to survive to
disk and resume. Writing that yourself is a durable execution engine.

And re-running is expensive: a crash at the sandbox step would repeat every
model call, which on a free tier is quota I cannot get back. Checkpointing is a
cost control here, not an aesthetic.

*If pushed on the downside:* another layer in every stack trace, an API that
moves between versions, typing friction that cost four documented ignores, and a
state migration problem I have not solved — change the schema and old
checkpoints no longer match.

---

> **Walk me through your RAG pipeline.**

Select which files are worth indexing at all — lock files and vendored code are
actively harmful, every irrelevant chunk is a chance to return the wrong thing.
Chunk on AST boundaries, so a function is never cut in half. Embed with a 67 MB
local model. Store in Qdrant embedded. Retrieve, then rerank, then assemble into
the prompt with citations.

Two decisions worth explaining. Chunking cuts on declarations because a function
split in half gives you two useless pieces — one with the name and no logic that
matches searches it cannot answer, and one with logic and no name that is
unfindable. And every chunk carries a header with its file, class and symbol,
because a bare `def get(self, key)` could be from anywhere.

---

> **You built hybrid retrieval and then turned it off. Why?**

Because I measured it. On 192 labelled examples, dense alone got Recall@5 of
0.835 against hybrid's 0.757, and reranking dropped it to 0.775 at 46 times the
latency.

I did not just record that. Reciprocal rank fusion weights both rankings
equally, which is only right if they are comparable — and mine were not, 0.835
against BM25's 0.609. So I swept the weighting, and the result climbed
monotonically toward dense-only: 0.757, 0.799, 0.817, 0.835. It converges rather
than peaking in the middle, which is the signature of one ranker contributing
nothing the other lacked — for this query shape, which is whole issue bodies.

I kept hybrid as a parameter, because that finding is about this corpus. If a
later phase queries with a symbol name instead of prose, BM25 should be
re-measured.

*And the honest caveat:* my top two configurations differ by less than one
standard error at n=192, so I do not claim dense beats hybrid-10:1.

---

> **Where does your ground truth come from?**

Git already recorded it. When a maintainer fixes issue #841, the commit says
"closes #841" and touches exactly the files that had to change. So every closed
issue is a labelled example produced as a side effect of normal development,
with nobody annotating anything.

Finding a free oracle is most of the work of evaluation — a hand-labelled set
gets built once and never refreshed.

---

> **What is wrong with that benchmark?**

Several things, and they all push the numbers down. The fix commit is one valid
answer, not the only one, so a genuinely relevant file that was not in it scores
as wrong — the numbers are a lower bound. Only issues closed by an identifiable
commit are usable, which may skew towards tidy fixes. Scoring is at file
granularity because that is what the labels support. And it is one repository,
which concentrates its changes in two files.

---

## Level 4 — Scale and cost

> **How would you scale this to 100,000 repositories?**

Four things break, in order.

BM25 breaks first. It is computed in-process, holding the corpus in memory and
rebuilt per repository. Fine for one; untenable for many. The fix is Qdrant's
sparse vectors so the inverted index lives in the database.

Then indexing throughput — 1,873 chunks took 504 seconds on CPU. Fine once per
repository, not fine 100,000 times. That wants GPU embedding and incremental
reindexing of changed files only.

Then the checkpointer: SQLite serialises writers, so concurrent agents contend.
PostgresSaver implements the same interface, so it is a constructor change
rather than a rewrite — which is most of why I did not hand-roll the storage.

Then the sandbox: one Docker daemon on one machine. That becomes a pool of
ephemeral runners, which is exactly what the deployment already does by
delegating validation to GitHub Actions.

---

> **How would you reduce cost?**

I would start by knowing where it goes, which is why accounting is per node
rather than per run. "This cost 12,000 tokens" is not actionable; "root cause
2,700, plan 1,800, patch 4,100, three repairs 3,400" says look at the loop.

Then in order: cache identical requests, which makes re-running free and is
already in place. Bound the loop on *progress* rather than just attempt count —
with a limit of five, an unchanging failure stops after two, so three calls are
not spent learning the same thing. Use a cheaper model for mechanical stages;
flash-lite answered the same prompt in 1.4 seconds against 5.3. And retrieve
fewer chunks, since more context is not better context.

---

> **What happens when GitHub rate-limits you?**

It mostly does not, because of conditional requests. I store the ETag and send
`If-None-Match`, and unchanged data comes back as a 304 that is not charged
against the budget — measured, a repeat fetch went from two requests to zero.
Plus `since=` for incremental sync, and reading the remaining budget from the
response headers so I slow down before being cut off rather than after.

When it does happen, the client distinguishes the primary limit — budget gone,
retrying is pointless — from a secondary limit, which means too fast and backing
off works. They both arrive as a 403, so the code branches on the header rather
than the status code.

---

## Level 5 — Security

> **You run untrusted code. How?**

In a container with every control set explicitly rather than inherited from
defaults: no network, read-only root with a tmpfs, memory equal to
memory-plus-swap so the limit cannot be escaped, a PID limit against fork bombs,
all capabilities dropped, no-new-privileges, a non-root user, and a wall-clock
timeout enforced from outside because Docker has none of its own.

The one that matters most is no network. A test that can reach the internet can
exfiltrate whatever the process can see.

And I would say the limitation out loud: a container shares the host kernel, so
a kernel exploit escapes it. For public repository tests I accept that — the
realistic threats are accidents and opportunistic exfiltration — but a targeted
attacker needs a VM or gVisor, and I would not imply the container covers that.

---

> **Installing dependencies needs the network. Doesn't that break your model?**

That is the actual centre of the design. Installation needs egress *and*
executes arbitrary code, because `setup.py` runs and so do build hooks. So it is
not a safe prelude to the dangerous part — it *is* a dangerous part.

Install and run are separate trust levels. Installation happens once, in its own
container, at image build. Every test run afterwards uses that image with no
network at all.

---

> **How do you handle prompt injection?**

You do not, completely, and that is the first thing worth saying. A model reads
one token stream and cannot reliably separate data from instructions, so unlike
SQL injection there is no parameterised-query equivalent. The question is not how
to prevent it but what an injection can accomplish.

So: layers, each assuming the one above failed. Untrusted content fenced and
labelled. Output constrained to a schema, so a successful injection cannot
escape into a different kind of action. A policy engine that denies edits to CI
config, `.ssh`, `.env` and credentials regardless of how convincing the text was.
A sandbox with no network. And a human.

Pattern-based detection is the weakest layer and I treat it as monitoring. There
is a test asserting an evasive phrasing gets through, so the limitation is
documented rather than assumed away.

---

> **If the model were fully compromised and hostile, what could it do?**

That is the right question. It could produce a schema-valid patch that is wrong.
It could not modify CI, write to `.ssh` or `.env`, commit a credential, or
disable a test — those are denied by a function, and functions do not read
English. It could not reach the network when the tests ran. And nothing gets
pushed without a person looking at a diff.

---

> **What is the difference between a prompt instruction and a guardrail?**

One is a request and the other is an `if`. "Never delete a test" in a system
prompt is something the model complies with when nothing is pushing it
otherwise. A function that inspects the patch and refuses when it introduces a
skip marker is a control. Injected text can argue with the first and cannot
argue with the second.

Every rule that matters in this project exists twice — stated in the prompt, and
enforced in code.

---

## Level 6 — Behavioural

> **Tell me about a difficult bug.**

*(Use S-007 — the benchmark that could never have scored 1.0.)*

I built a retrieval benchmark with free ground truth from git history. Before
trusting the numbers I printed three of the labels, and saw that each example
had about two files and roughly half were *test* files — because a fix commit
touches the source and its test together.

My retriever excludes test files, deliberately. So for those examples it was
forbidden from ever returning half the correct answers. Maximum achievable
recall was capped below 1.0, at a different level per example.

Nothing failed. It would have printed 0.62 and I would have optimised against a
ceiling I did not know existed. I killed the run — 24 minutes in — filtered test
files out of the labels, and added two regression tests.

*What it taught me:* measurement bugs are the dangerous kind because they produce
a number rather than an error. The check I now apply is that the labels and the
system under test must agree on what a valid answer is.

---

> **Tell me about a time the data contradicted your design.**

*(S-006 / ADR-012 — hybrid retrieval.)*

I built hybrid retrieval with reranking because it is the standard architecture
and the reasoning is genuinely good — vector search and keyword search fail in
opposite directions. Then I measured it and dense alone beat it, while reranking
made things worse at 46 times the latency.

Rather than accept or dismiss that, I tested the likely explanation: rank fusion
weights both rankings equally, so fusing a strong ranker with a weak one should
drag the strong one down. I swept the weighting and it climbed monotonically
toward dense-only. So I changed the default, kept hybrid as a parameter because
the finding is corpus-specific, and wrote down that my top two results differ by
less than one standard error.

---

> **Tell me about a production failure.**

*(S-009 — the 503.)*

The first live end-to-end run failed. The model I had configured had been retired
and returned 404, and its replacement returned 503 for several minutes.

My retry logic worked exactly as designed — backoff, four attempts, clean halt
with a reason — and was useless, because retrying one overloaded model harder
does not make it available. So I checked whether the provider or that model was
down, found two other models serving the same prompt fine in the same minute, and
built a fallback chain.

The part I was careful about was what *not* to fall back on: a 400 or a schema
violation fails identically everywhere, and falling back there would turn a loud
fixable error into a slow hidden one.

---

> **Tell me about a mistake you made.**

*(F-009 — the leaked key fixture. Use this one; it is the most honest.)*

I needed a string shaped like a Google API key for the test proving the policy
engine blocks credentials. I built it by taking the real key and swapping two
hyphens for digits — which left 28 of its 39 characters identical, and that went
to a public repository.

Any string of the right shape would have worked. The real key carried no benefit,
only risk.

I replaced it with obviously synthetic values, advised rotating the key —
removing it from the tip does not un-publish it — and recorded it in the failure
log. The part worth keeping is the irony: this is a project whose policy engine
denies patches that add credentials, with tests proving it, and the credential
got in through *the test for that rule*. A control does not cover the code that
tests it.

Rotating the key then surfaced a second gap: the new key used a format my own
detector did not recognise. So I added a shape-based rule alongside the
vendor-prefix list, because a denylist always lags.

---

> **Tell me about a time you made something less capable on purpose.**

*(S — the MCP decision.)*

I built an MCP server so other agents could use PatchPilot's tools, and exposed
only the read-only half. The tempting argument was that mutating tools would
still hit my human-approval step, so exposing them was safe. That is wrong: an
MCP client is an arbitrary program calling tools autonomously, so the approval
node would still run and would be protecting nobody — its value came from a
person looking at a diff.

So the server can investigate an issue and cannot fix one.

---

> **What would you do differently?**

Write the comparison test earlier. I had tests for caching and tests for
pagination, both passing, while the interaction between them silently lost a
fifth of the data. Four of the nine bugs in my failure log are that exact shape —
individually correct components, wrong together — which is why I would push more
weight onto integration-shaped tests from the start, even though they are slow.

I would also have run the end-to-end benchmark earlier, before quota became the
constraint. The retrieval numbers are solid; the fix-rate numbers do not exist
yet, and that is a gap I created by sequencing badly.

---

## The three questions to be ready for

**"What is your success rate?"** — *I do not have one yet.* The harness exists
and is tested; the benchmark has not been run because free-tier quota ran out.
One correct patch on sqlite-utils #841 is a data point, not a rate, and quoting
a rate from it is exactly the failure my own log exists to prevent.

That answer is stronger than a number would be. It demonstrates the discipline
the whole project is about, and an interviewer who hears it will trust every
other number you give them.

**"Did you build this yourself?"** — Be straightforward. You directed it, made
the decisions, and can defend every one. The ADRs are the evidence: each records
the alternatives that were actually considered and the tradeoff taken. Knowing
*why* dense beat hybrid on your corpus is not something you can fake.

**"What is the weakest part?"** — The end-to-end evaluation, for the reason
above. After that, the ranking weights are guesses — stable, tunable guesses, but
guesses, and I already have evidence they need tuning: two issues scored
"attempt" despite being over three years stale.
