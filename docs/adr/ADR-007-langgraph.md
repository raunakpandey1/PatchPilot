# ADR-007 — LangGraph for orchestration

**Status:** Accepted · **Phase:** 2 · **Date:** 2026-09-18

## Context

PatchPilot's eventual control flow is not a straight line:

- a debug loop — `generate → test → debug → generate` — repeating up to a bound
- a pause for human approval, which may be answered hours later from a
  *different process*
- early exits at several points (repository unavailable, no testable command, no
  actionable issue), each needing to report *why* rather than just stopping

Today's graph is four nodes in a row. A `for` loop would do it. The decision has
to be made against where this is going, because moving orchestration later means
rewriting every node.

## Options considered

1. **A plain `while`/`for` loop.**
2. **A hand-written state machine** — explicit states, a transition table, and
   my own serialisation.
3. **LangChain `AgentExecutor`** — the classic ReAct loop.
4. **CrewAI / AutoGen** — multi-agent frameworks built around agents conversing.
5. **Temporal or Prefect** — durable workflow engines.
6. **A hosted agent API** — let a provider run the loop.

## Decision

**LangGraph**, with a SQLite checkpointer ([ADR-009](ADR-009-sqlite-checkpointer.md)).

## Reasoning

Three properties of *this* project decide it. None is "it is popular".

**1. The control flow is a bounded cycle, not a line.** A loop can express that.
What a loop cannot do cleanly is keep the loop condition, the attempt counter,
the failure summarisation and the actual work separable. LangGraph makes the
cycle a conditional edge, so "when do we go back?" is a small pure function:

```python
def continue_or_halt(state) -> Literal["continue", "halt"]:
    return "halt" if state.get("halted") else "continue"
```

Testable with a dictionary. In the loop version it is an early `return` in the
middle of a function that also does the work.

**2. Human approval spans process boundaries.** The approval arrives later,
possibly from a second CLI invocation after the laptop slept. That requires the
whole agent state serialised to durable storage and resumed at exactly the right
step. `interrupt()` plus a checkpointer does this. Building it means building a
durable execution engine.

**3. Re-running is expensive here.** A crash at the sandbox step on 8 GB of RAM
must not force a re-clone, re-index and re-investigation — that burns free-tier
model quota we cannot replace. Checkpointing is a **cost control** in this
project, not an aesthetic. This is the specific answer to "why not just a loop?"

## Why not the alternatives

| Option | Why not |
|---|---|
| Plain loop | Fine until cycles, resume and branching all appear; then control flow and domain logic braid together, and there is no state to resume from |
| Hand-written state machine | The right *idea* — LangGraph is one, with serialisation, history and tracing already built. Writing it means owning a durable execution engine as a side project |
| LangChain `AgentExecutor` | One ReAct loop, no typed state, no checkpointing, no bounded cycles. Cannot express the debug loop or the approval pause |
| CrewAI / AutoGen | Built for agents *conversing*, with emergent control flow. PatchPilot must be auditable — a security review has to state exactly which node can touch git. Emergent routing gives that up |
| Temporal / Prefect | Genuinely correct for durable execution, and the honest answer at scale. Needs a server plus workers, has no LLM-native tracing, and does not fit 8 GB |
| Hosted agent API | Costs money; the orchestration becomes a black box, so there is nothing to inspect, test or explain |

## Tradeoffs

**Against:**

- **Another layer to debug through.** Stack traces pass through framework code.
- **A fast-moving API.** Documentation drifts; imports move between versions.
- **Typing friction, measured.** `mypy --strict` rejects `add_node` when the
  node comes from a factory returning a `Callable` alias. Reduced to a six-line
  file with no project code: a directly-defined function is accepted, a returned
  callable is not. Four documented `type: ignore[call-overload]` comments —
  see [failures.md](../failures.md) F-004.
- **State migration.** Change the state schema and existing checkpoints were
  written with the old shape. There is no migration story. In development the
  answer is deleting the checkpoint file; in production it is a real problem.

**For:** durable state for free; cycles and routing as declarative structure;
per-step history for debugging; a direct path to human-in-the-loop in Phase 9
and to LangSmith tracing in Phase 10.

## Consequences

- Everything in `AgentState` must be serialisable — so live clients live in
  [`deps.py`](../../src/patchpilot/agent/deps.py) and are closed over by nodes.
- Accumulating fields need reducers; forgetting one loses data silently. There
  is a test for it.
- Phase 7's debug loop is a conditional edge, not a `while`.
- Phase 9's approval is `interrupt()` plus the same checkpointer.

## Interview questions

**Q: Why LangGraph and not a plain loop?**

Three things a loop does not give me. The debug loop is a bounded cycle, and as
a conditional edge the routing decision stays a small pure function instead of
tangling with the work. Human approval spans process boundaries, so the whole
state has to survive to disk and resume — which is a durable execution engine if
you write it yourself. And re-running is expensive: a crash at the sandbox step
would otherwise mean repeating every model call, which on a free tier is quota I
cannot get back. Checkpointing is a cost control here.

**Q: Why not CrewAI or AutoGen?**

They are built around agents conversing, with control flow that emerges from the
conversation. PatchPilot edits other people's repositories, so a security review
has to be able to state exactly which node is capable of touching git. Emergent
routing makes that unanswerable. I want most of the control flow fixed and the
model used at specific points for specific judgements.

**Q: What does LangGraph cost you?**

An extra layer in every stack trace, an API that moves between versions, typing
friction I had to work around with four documented ignores, and a state
migration problem — change the schema and old checkpoints no longer match, with
nothing to migrate them. In development I delete the checkpoint file; in
production that is a genuine design cost I would have to solve.

**Q: When would you use Temporal instead?**

When durability matters more than LLM-specific tooling and I can run a server —
many concurrent long-running workflows, retries and timeouts as first-class
infrastructure, a team already operating it. It is the more serious durable
execution engine. It is also a server plus workers, which does not fit an 8 GB
laptop, and it has no notion of prompts, tokens or traces.

## Behavioural question this answers

> *"Tell me about a time you chose a framework over building it yourself."*

The alternative to LangGraph was a hand-written state machine, and I was tempted
because I would have understood every line. What changed my mind was listing
what I would actually have to write: per-step state serialisation, resume by run
id, checkpoint history, and conditional routing — a durable execution engine, as
a side project, inside a project about agents. I took the framework and wrote
down its costs explicitly in the ADR, including the typing friction I hit and the
state-migration problem I have not solved. Naming the costs up front is what made
it a decision rather than a default.
