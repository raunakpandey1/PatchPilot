# Checkpointing: how an agent pauses and comes back

## In one sentence

A checkpointer writes the agent's entire state to a database after every step,
so a run can be stopped — by a crash, or on purpose to wait for a human — and
continued later from exactly where it was.

## The problem it solves

Two concrete situations in PatchPilot.

**A human has to approve the patch.** The agent produces a diff and stops. The
person looks at it after lunch, or tomorrow, from a completely separate run of
the CLI. The original process is long gone.

**Something crashes at step 9 of 12.** On an 8 GB laptop, Docker running a test
suite can exhaust memory. Without checkpoints, recovery means starting over:
re-clone, re-index the repository, re-run every model call. Those model calls
cost free-tier quota that does not come back.

Both need the same thing: the run's state, on disk, resumable.

## How it works, step by step

### 1. After every node, the state is saved

```python
with SqliteSaver.from_conn_string("workspace/checkpoints.sqlite") as saver:
    compiled = graph.compile(checkpointer=saver)
```

That is the entire setup. Now, after each node finishes, LangGraph serialises the
state and writes it with the run's `thread_id` and a step number.

Our four-node run produces more than four checkpoints — LangGraph records the
boundaries around each step, not just the results.

### 2. `thread_id` identifies the run

```python
config = {"configurable": {"thread_id": "run_abc123"}}
compiled.invoke(initial_state(...), config=config)
```

`thread_id` is the key everything is stored under. Two runs with different
thread ids share a database and cannot see each other's state — tested in
`test_separate_threads_do_not_share_state`.

### 3. Resuming is invoking again with the same thread id

Pass the same `thread_id` and LangGraph loads the saved state and continues from
the next step rather than starting over.

You can also just *look*:

```python
snapshot = compiled.get_state(config)
snapshot.values["visited"]      # ['analyze_repository', 'discover_issues', ...]
snapshot.next                   # which nodes would run next
```

We verified this the strongest way available — a **different graph object**
reads back a run it never executed:

```python
build_graph(deps, checkpointer=saver).invoke(initial_state(...), config=config)

recovered = build_graph(deps, checkpointer=saver).get_state(config)
assert recovered.values["selected_issue"].number == 1
```

Nothing was held in memory by the graph that ran it. The state is genuinely in
the store. That is `test_state_survives_into_the_checkpointer`.

### 4. History is kept, not just the latest state

```python
list(compiled.get_state_history(config))   # every checkpoint, newest first
```

Which means you can inspect what the state looked like at step 3 — or resume
from there. For debugging an agent, "what did it know when it made that
decision?" is often the only question that matters.

## Why this is a cost control, not just an elegance argument

It is tempting to file checkpointing under "nice architecture". In this project
it is a budget decision.

A full run will eventually be: clone (seconds), index the repository (a minute
and an embedding pass), investigate (several model calls), generate a patch
(more calls), run tests in Docker (a minute). If that dies at the last step,
restarting without checkpoints repeats every model call — spending free-tier
quota to recompute an answer already obtained.

With checkpoints, a crash costs one node.

## In PatchPilot

- [`agent/graph.py`](../../src/patchpilot/agent/graph.py) —
  `sqlite_checkpointer()` for real runs, `memory_checkpointer()` for tests.
- [`config.py`](../../src/patchpilot/config.py) — `checkpoint_db`, defaulting to
  `workspace/checkpoints.sqlite`.
- [`tests/unit/test_graph.py`](../../tests/unit/test_graph.py) — resume, history
  and thread isolation.

SQLite rather than Postgres because this runs on a laptop with 8 GB of RAM and a
checkpointer is not where to spend a gigabyte on a database server. The interface
is the one Postgres also implements, so moving is a constructor change —
[ADR-009](../adr/ADR-009-sqlite-checkpointer.md).

## What goes wrong

**Reusing a `thread_id` by accident.** You think you started a fresh run; you
resumed an old one and got its stale state. Use a new id per run — we generate
`run_<uuid>`.

**Changing the state schema while checkpoints exist.** Old rows were written
with the old shape and nothing migrates them. The practical answer during
development is to delete the checkpoint file; in production it is a real
migration problem and a genuine cost of this design.

**Putting unserialisable things in state.** An open socket or file handle fails
at checkpoint time, which is a confusing distance from where you wrote it.

**Assuming checkpoints are a cache.** They record what happened in a *specific
run*. They are not shared between runs and do not deduplicate work across them.

## Interview questions

**Q: How does an agent pause for human approval and resume hours later?**

The graph's state is serialised to a checkpoint store after every step, keyed by
a `thread_id`. The run stops and the process can exit entirely. When approval
arrives, a new invocation with the same `thread_id` loads that state and
continues from the next step. It works because state is data rather than local
variables — which is the main reason the agent is a graph at all.

**Q: Why not just write your own JSON file?**

You can, and then you are maintaining checkpoint-per-step semantics, history,
serialisation of nested models, and concurrent access. That is a durable
execution engine, which is a project of its own. It is also exactly the argument
for Temporal — which is the right tool at a scale where you can afford to run a
server.

**Q: What does checkpointing cost?**

Write amplification — every step serialises the whole state — and a schema
migration problem, because old checkpoints were written with the old shape and
nothing migrates them. It also makes state size a real concern: a state holding
every retrieved chunk gets rewritten on every step.

**Q: How do you know it actually works?**

A test where one graph object runs the agent and a *different* graph object
reads the state back and asserts the selected issue is still there. If anything
were being held in memory by the running graph, that test would fail.

## Official documentation

- [LangGraph — Persistence](https://langchain-ai.github.io/langgraph/concepts/persistence/) — threads, checkpoints, state history.
- [LangGraph — Add persistence](https://langchain-ai.github.io/langgraph/how-tos/persistence/) — the practical setup.
