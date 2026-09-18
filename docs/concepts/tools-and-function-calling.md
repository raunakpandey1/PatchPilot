# Tools: letting a model do things

## In one sentence

A tool is a function you describe to a model so it can ask for it to be called —
and the model never runs anything itself, which is the entire security model.

## The problem it solves

A model can only produce text. To find out whether a patch works, something must
actually run the tests.

Tool use is the mechanism: you describe available functions, the model responds
with "call `run_tests` with these arguments", **your code** decides whether to do
it, does it, and puts the result back in the conversation.

That indirection is not incidental. The model emits a *request*; your program
decides. Every safety property in PatchPilot depends on there being a decision
point there.

## How it works, step by step

```
1. you describe tools        name, description, JSON Schema
2. model responds            "call search_repository(query='rows_where')"
3. YOUR CODE decides         policy check, authorization, rate limit
4. your code executes        the actual function
5. result goes back          as a message the model reads
6. model continues           with information it could not have invented
```

Step 3 is the one people skip. A loop that executes whatever the model asked for
has given the model your permissions.

## The design decision PatchPilot made differently

Most agent tutorials give the model a set of tools and let it choose freely in a
loop. PatchPilot mostly does not.

The sequence — analyse, discover, rank, retrieve, diagnose, plan, patch, test —
is **fixed in the graph**. The model is called at specific points for specific
judgements, each returning a declared schema. It does not choose what to do next.

That is a real trade, and worth being able to defend both ways:

**What it costs:** the agent cannot improvise a step nobody anticipated. If an
issue needs something outside the pipeline, it fails rather than adapting.

**What it buys:**

- **Auditability.** A security review can be told exactly which node can touch
  git. With free tool choice the honest answer is "any of them, potentially".
- **Debuggability.** A failed run is compared against a known path.
- **Cost.** Deciding what to do next is itself a model call. Not making it,
  thousands of times, is free — which on a free tier is the difference between
  running the benchmark and not.

For an agent that edits other people's repositories, that trade is the right way
round. For an open-ended research assistant it would not be.

## Structured output is the same idea, narrowed

`complete_structured(messages, schema=RootCause)` is tool use with exactly one
tool that must be called. The mechanism underneath is the same — a declared
schema the model must conform to — which is why both give you the same
guarantee: the *shape* is enforced by the decoder rather than requested in
English.

See [structured output](structured-output.md).

## Designing a tool well

**The description is the API.** A model picks a tool by reading it. Say when to
use it and what comes back, not just what it is.

**Schemas should be narrow.** `run_command(cmd: str)` is a shell. `run_tests()`
is a tool. Every parameter is something an attacker or a confused model can
influence.

**Return data, not prose.** The result goes back into a prompt; structure
survives, formatting does not.

**Errors are results.** A tool that could not do its job should return that as
data, so the model can reason about it. Raising makes it a program crash rather
than something to handle.

**Idempotency where you can get it.** Retries happen. A tool called twice should
not do the thing twice.

## In PatchPilot

- [`tools/`](../../src/patchpilot/tools/) — github, git, workspace, patching.
  Ordinary Python, no model involved.
- [`llm/base.py`](../../src/patchpilot/llm/base.py) — the model interface, which
  deliberately exposes only text and schema-constrained output.
- [`mcp/server.py`](../../src/patchpilot/mcp/server.py) — the same tools
  described to *other* agents, read-only ones only.

Note what is missing: there is no `run_shell_command`. The repository's test
command is discovered deterministically and executed in a sandbox as an argument
list, never assembled by a model and never passed to a shell.

## What goes wrong

**Executing whatever the model asked for.** The loop with no step 3.

**A shell as a tool.** `run_command(cmd: str)` hands over everything. Every
specific tool you add instead is a capability you chose.

**String commands instead of argument lists.** A string needs a shell to run it,
and a shell interprets `;`, `&&` and backticks — in a value influenced by
untrusted content.

**Unbounded tool loops.** "Model decides when to stop" is not a stopping
condition. PatchPilot bounds its one loop three ways
([the debug loop](what-is-an-agent.md)).

**Descriptions that restate the signature.** The model then guesses when to use
the tool, and guesses badly.

## Interview questions

**Q: How does tool use actually work?**

You describe functions with a name, a description and a JSON Schema. The model
responds with a request to call one and some arguments. Your code then decides
whether to honour it, executes it, and feeds the result back as a message. The
model never executes anything — it asks, and your program decides. That decision
point is where authorization lives, and a loop that skips it has handed the model
its own permissions.

**Q: How much freedom did you give the model over tool choice?**

Very little, deliberately. The step sequence is fixed in the graph and the model
is called at specific points for specific judgements with declared output
schemas. That costs improvisation and buys three things: a security review can
be told exactly which node can touch git, a failed run can be compared against a
known path, and I am not paying for a model call to decide what to do next. For
something that edits other people's repositories that is the right trade; for an
open-ended assistant it would not be.

**Q: Why is there no shell tool?**

Because `run_command(cmd: str)` is not a tool, it is a shell, and exposing it
hands over everything the process can do. The test command is discovered by
reading the repository's config files, kept as an argument list so no shell ever
interprets it, and executed inside a container with no network. Each specific
tool is a capability I chose rather than one I failed to exclude.

**Q: What makes a good tool description?**

It tells the model when to use the tool and when not to, and what it gets back —
because the description *is* the interface the model programs against. A
description that restates the type signature leaves the model guessing, and it
will guess.

## Official documentation

- [Gemini API — Function calling](https://ai.google.dev/gemini-api/docs/function-calling) — declarations, the call/response cycle, and parallel calls.
- [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents) — when a fixed workflow beats free tool choice.
