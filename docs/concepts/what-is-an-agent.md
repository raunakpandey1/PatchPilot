# What an "agent" actually is

## In one sentence

An agent is a loop in which a language model decides what to do next, something
external actually does it, and the result feeds back in — repeating until the
job is done or you stop it.

## The problem it solves

A single model call is a good translator and a bad worker. Ask one to "fix issue
#841" and you get a plausible-looking patch produced without ever reading the
code, running anything, or finding out whether it worked.

Fixing a bug is not one question. It is:

```
find the relevant code → form a hypothesis → write a change →
run the tests → read the failure → revise → run again
```

Each step depends on what the previous step *actually produced* — not on what
was predicted. That dependency is the loop, and the loop is the agent.

## How it works, step by step

Strip away the frameworks and every agent is this:

```python
while not done:
    action = model.decide(state)      # think
    result = execute(action)          # act
    state = update(state, result)     # observe
```

Three parts, and each maps onto something concrete in PatchPilot:

| Part | Means | Here |
|---|---|---|
| **think** | a model call producing a decision | root cause, fix plan, patch |
| **act** | code with real effects | run tests in Docker, read a file, search the index |
| **observe** | feeding the real result back | test output becomes the next prompt |

**The "act" step is where the value is.** A model guessing whether its patch
works is worthless. A model *told* that 3 tests failed with a specific
`AssertionError` has information it could not have invented.

### What makes this hard

**It can loop forever.** Nothing in the mechanism says "give up". Phase 7 bounds
iterations explicitly.

**Errors compound.** A wrong root cause produces a wrong plan produces a wrong
patch. Three confident steps, each built on the last mistake.

**Cost is unbounded by default.** Each iteration is more tokens. A loop that
runs ten times costs ten times as much, and on a free tier that is a quota you
do not get back.

**It is hard to debug.** When a run goes wrong you need to know what it did, in
order, with what inputs — which is why state, tracing and the `visited` list
exist.

### The spectrum, and where PatchPilot sits

```
 fixed pipeline ────────────────────────────────► fully autonomous
 every step in code                          model decides everything
 predictable, limited                        flexible, unpredictable
        ▲
        │ PatchPilot is deliberately near this end
```

Most of PatchPilot's control flow is **fixed**. The order — analyse, discover,
rank, investigate, patch, test — is written in the graph, not chosen by a model.
The model is used at specific points for specific judgements, each with a
declared output schema.

That is a deliberate constraint, and it is worth being able to defend:

- **Auditable.** A security review must be able to state exactly which node can
  touch git. With emergent control flow, the honest answer is "any of them,
  potentially".
- **Debuggable.** When a run fails you compare against a known path.
- **Cheaper.** Deciding what to do next is itself a model call. Not making that
  call, thousands of times, is free.

The cost is flexibility: PatchPilot cannot invent a step nobody anticipated.
For an agent that edits other people's repositories, that is the right trade.

## In PatchPilot

- [`agent/graph.py`](../../src/patchpilot/agent/graph.py) — the loop, as a graph.
- [`agent/state.py`](../../src/patchpilot/agent/state.py) — what it knows.
- [`agent/nodes/`](../../src/patchpilot/agent/nodes/) — one node per step.

Today's graph is a straight line — analyse → discover → rank → select. The
cycle arrives in Phase 7.

## What goes wrong

**No stopping condition.** Always bound the iterations, and log which bound was
hit.

**Feeding back a summary instead of the real output.** If the model summarises
the test failure and you feed it back the summary, the loop is now reasoning
about its own description of reality. Feed back the actual output.

**Letting the model decide everything because it can.** "The model figures it
out" is a design that cannot be tested, audited or costed.

**Confusing an agent with a chatbot.** A chatbot's environment is a person. An
agent's environment is a filesystem, a test runner, an API — things that fail in
ways a conversation does not.

## Interview questions

**Q: What is an agent?**

A loop where a model decides an action, real code executes it, and the actual
result feeds back into the next decision. The defining feature is the feedback
from a real environment — that is what separates it from a single call or a
fixed pipeline. In PatchPilot the environment is a git repository and a test
runner, so the agent finds out whether its patch works rather than predicting it.

**Q: How much autonomy did you give it, and why?**

Deliberately little. The sequence of steps is fixed in the graph; the model is
used at specific points for specific judgements, each returning a declared
schema. That makes it auditable — I can say exactly which node is capable of
touching git — debuggable, and cheaper, because choosing the next step is itself
a model call I am not making. The cost is that it cannot improvise a step I did
not anticipate, which for something that edits other people's repositories is
the trade I want.

**Q: What stops it looping forever?**

An explicit iteration bound, plus a cost ceiling. Nothing in the mechanism
produces "I give up" on its own — a model asked to try again will always try
again. The limit is in the graph's routing function, and hitting it is recorded
as an outcome rather than a crash.

**Q: Where do agents most often fail?**

Compounding errors. A wrong root cause yields a confident wrong plan and a
confident wrong patch — three steps that all look fine. The defence is
verification against something real at each stage: retrieval grounds the
analysis in actual code, and the test suite decides whether the patch worked.
Not the model's opinion of whether it worked.

## Official documentation

- [LangGraph — Why LangGraph?](https://langchain-ai.github.io/langgraph/concepts/high_level/) — the framework's own framing of agent control flow.
- [Anthropic — Building effective agents](https://www.anthropic.com/research/building-effective-agents) — when a workflow beats an agent, and why simple usually wins.
