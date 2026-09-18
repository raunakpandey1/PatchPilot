# Phase 2 — LangGraph and the LLM abstraction

**Goal:** turn four functions into an agent — a graph with durable state, a
swappable model behind an interface, and a deterministic way to choose what to
work on.
**Date:** 2026-09-18

## 1. What we built

```
START
  │
  ▼
analyze_repository ──halted──► END        (repo missing / not testable)
  │
  ▼
discover_issues ─────halted──► END        (no open issues)
  │
  ▼
rank_issues                               (deterministic, explainable)
  │
  ▼
select_issue ────────────────► END        (or halts: nothing actionable)
```

| Module | Responsibility |
|---|---|
| [`llm/base.py`](../../src/patchpilot/llm/base.py) | the provider interface — two methods, plus usage accounting |
| [`llm/gemini.py`](../../src/patchpilot/llm/gemini.py) | the real provider, with schema-constrained output |
| [`llm/fake.py`](../../src/patchpilot/llm/fake.py) | scripted, deterministic, used by all 121 tests |
| [`agent/state.py`](../../src/patchpilot/agent/state.py) | the state, and the reducers that make it accumulate |
| [`agent/ranking.py`](../../src/patchpilot/agent/ranking.py) | issue scoring — arithmetic, not an LLM |
| [`agent/deps.py`](../../src/patchpilot/agent/deps.py) | the live objects that cannot live in state |
| [`agent/nodes/`](../../src/patchpilot/agent/nodes/) | four nodes |
| [`agent/graph.py`](../../src/patchpilot/agent/graph.py) | assembly, routing, checkpointers |

## 2. How it works

**State is a value, not local variables.** A `TypedDict` that gets serialised to
SQLite after every step. That is what makes a run resumable from a different
process — the thing Phase 9 needs. Fields that must accumulate (`visited`,
`errors`, `usage`) declare reducers; without them, two nodes writing the same
key would leave only the second. See
[langgraph-state-nodes-edges](../concepts/langgraph-state-nodes-edges.md).

**Live objects are passed in at build time.** The GitHub client and the model
live in `AgentDeps` and are closed over by nodes, because a socket does not
survive serialisation. The side effect is that tests substitute a whole world.

**Halting is data.** Any node can return `halted=True` with a reason, and one
small pure function routes on it:

```python
def continue_or_halt(state) -> Literal["continue", "halt"]:
    return "halt" if state.get("halted") else "continue"
```

"When does the agent stop?" is now testable with a dictionary.

**The model is behind a two-method protocol.** `complete` and
`complete_structured`. One factory names a concrete provider; nothing else does.
See [ADR-008](../adr/ADR-008-llm-provider-abstraction.md).

**Ranking is arithmetic.** Six weighted factors, plus hard blockers that
short-circuit. Every score carries its derivation. See
[ADR-010](../adr/ADR-010-deterministic-ranking.md).

## 3. Why this way

The graph is a straight line today; a `for` loop would do it. It is a graph
because of where this is going — a bounded debug cycle in Phase 7 and a pause
for human approval in Phase 9, both of which need state that survives to disk.

The sharpest version of the argument is not elegance, it is cost: a crash at the
sandbox step must not force a re-clone, re-index and re-investigation, because
that spends free-tier model quota we cannot replace. Checkpointing is a budget
control here. [ADR-007](../adr/ADR-007-langgraph.md).

## 4. Alternatives

| Decision | Alternatives | ADR |
|---|---|---|
| LangGraph | plain loop, hand-written state machine, AgentExecutor, CrewAI/AutoGen, Temporal | [007](../adr/ADR-007-langgraph.md) |
| Own LLM protocol | Gemini SDK directly, LangChain chat models, a gateway | [008](../adr/ADR-008-llm-provider-abstraction.md) |
| SQLite checkpointer | in-memory, Postgres, Redis | [009](../adr/ADR-009-sqlite-checkpointer.md) |
| Deterministic ranking | LLM ranking, hybrid, first `good first issue` | [010](../adr/ADR-010-deterministic-ranking.md) |

## 5. Tradeoffs

- **A framework layer to debug through**, an API that moves between versions,
  and typing friction that cost four documented `type: ignore` comments.
- **No state migration story.** Change the state schema and existing checkpoints
  were written with the old shape. In development, delete the file. In
  production this is unsolved, and saying so is better than pretending.
- **SQLite means one writer.** Concurrency is out of scope until that changes.
- **Ranking weights are guesses.** Stable guesses, which is the point — Phase 11
  can tune them against measured success.

## 6. Problems encountered

**F-004 — `mypy --strict` rejected valid LangGraph code.** Four errors on
`add_node`, while the tests passed and the graph ran correctly. Full write-up in
[failures.md](../failures.md); the short version is below.

Also two smaller frictions, fixed in passing:

- A `dataclass` refuses a mutable default, and `Settings` is a Pydantic model —
  `AgentDeps.settings` needs a `default_factory`.
- `httpx` logs one line per request at INFO, which buried our own events. Raised
  to WARNING in `setup_logging` — for `httpx`, `httpcore`, `google_genai` and
  `urllib3`.

## 7. Debugging process — F-004

**Symptom.** `mypy` reported no matching overload for `add_node`. Tests passed.
The real run worked.

**The temptation.** Add `# type: ignore` and move on. That is reasonable *if you
know whose bug it is* — and a silenced error hiding a real mistake is worse than
the error.

**Investigation.** Reduce it to the smallest file containing none of my own
code. A directly-defined `def node(state: S) -> S` is accepted by
`mypy --strict`. The identical function returned from a factory typed
`Callable[[S], S]` is rejected. Same types, same body; the only difference is
whether mypy sees a `def` or a value.

**Root cause.** LangGraph's overloads infer their node type parameter from a
directly-defined function and cannot infer it through a `Callable` alias. Our
nodes come from factories, because they close over dependencies — so all four
hit it.

**Fix.** Four `type: ignore[call-overload]`, each with the reduction recorded
beside it in the code.

**Verification.** `mypy --strict` clean; 121 tests pass; the graph runs against
the real repository.

**The transferable part:** before silencing a type error, reduce it to a file
containing none of your own code. If the minimal case still fails, it is theirs.
If it passes, it is yours — and you just found it.

## 8. Tests

121 unit tests, ~7.4 s, no network, no API key, no model calls.

| file | what it covers |
|---|---|
| [`test_ranking.py`](../../tests/unit/test_ranking.py) | 25 tests. Blockers gate rather than penalise; reproducibility is the heaviest factor; identical inputs give byte-identical output |
| [`test_llm.py`](../../tests/unit/test_llm.py) | 14 tests. Both providers satisfy the protocol; schema violations raise; the last scripted response repeats forever |
| [`test_graph.py`](../../tests/unit/test_graph.py) | 13 tests. Full graph offline; every halt path; reducers accumulate; **a different graph object reads a completed run back out of the checkpointer** |

Three worth singling out:

- `test_blockers_short_circuit_scoring` — a perfect-but-assigned issue scores
  exactly 0.0, not "high, minus a bit".
- `test_the_last_response_repeats_forever` — the fake keeps answering past the
  end of its script, which is how Phase 7 will test that a debug loop gives up.
- `test_state_survives_into_the_checkpointer` — one graph runs, a *different*
  graph reads the state back. If anything were held in memory, this fails.

## 9. Metrics

Full detail in [metrics.md](../metrics.md). On `simonw/sqlite-utils`:

**77 open issues → 10 attempt, 37 maybe, 30 skip**, ranked in under 3 ms, using
**zero model tokens**. The whole phase is deterministic.

Top pick was #841 at 0.91 — a labelled bug with a reproduction, recent activity
and one confirming comment.

**One honest observation:** #399 and #430 scored `attempt` at 1,220 and 1,556
days stale. The staleness weight looks too low. Recorded as a hypothesis for
Phase 11 to measure rather than something to tune by intuition now.

## 10. Official documentation

- [LangGraph — Low-level concepts](https://langchain-ai.github.io/langgraph/concepts/low_level/) — state, nodes, edges, reducers. The single most useful page.
- [LangGraph — Persistence](https://langchain-ai.github.io/langgraph/concepts/persistence/) — threads, checkpoints, and `get_state_history`.
- [Gemini API — Structured output](https://ai.google.dev/gemini-api/docs/structured-output) — `response_schema` and its limits.
- [PEP 544 — Protocols](https://peps.python.org/pep-0544/) — why the provider interface is not a base class.

## 11. Interview questions

1. What is agent state, and why can it not be local variables?
2. What is a reducer? What breaks without one?
3. Why is the GitHub client not in the state?
4. Why LangGraph rather than a `while` loop — and what does the framework cost?
5. How does an agent pause for approval and resume hours later?
6. Why rank issues with arithmetic instead of a model?
7. Your ranking weights are guesses. Why is that acceptable?
8. Why is an assigned issue scored zero rather than penalised?
9. Why abstract the LLM provider? Is that not premature?
10. `mypy` rejected working code. How did you establish whose bug it was?

## 12. STAR stories

**S-005** (minimal reproduction to decide whose bug it is) and **S-006**
(determinism upstream as a precondition for measurement downstream). See the
[story bank](../interview/story-bank.md).
