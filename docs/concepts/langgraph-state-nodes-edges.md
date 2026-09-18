# LangGraph: state, nodes, edges and reducers

## In one sentence

LangGraph turns an agent from a function with local variables into a state
machine whose state can be written to disk, which is what makes pausing,
resuming and looping possible.

## The problem it solves

Start with the plain version:

```python
def run(repo_name):
    repository = github.get_repository(repo_name)
    snapshot = analyze(clone(repository))
    issues = github.list_issues(repo_name)
    ranked = rank(issues)
    return select(ranked)
```

Perfectly good. It stops being good when you need any of these:

**Pause for a human and resume later.** A person approves a patch minutes or
hours later, possibly from a second run of the CLI. The function's local
variables are gone when it returns. You would have to invent a way to write them
all down and reconstruct them.

**Survive a crash without redoing the expensive parts.** If the sandbox runs out
of memory at step 9, you do not want to re-clone, re-index and re-investigate.
On a free tier, redoing model calls costs quota you do not get back.

**Loop with a bound.** `generate → test → debug → generate`, up to five times.
Expressible as a `while`, but the loop condition, the attempt counter, the
failure summarisation and the actual work end up in one function nobody can test
in pieces.

All three need the working data to be an explicit **value** rather than local
variables. That is what LangGraph provides, plus the machinery around it.

## How it works, step by step

### 1. State — one typed dictionary that is the run

```python
class AgentState(TypedDict, total=False):
    run_id: str
    repository: Repository | None
    candidate_issues: list[Issue]
    visited: Annotated[list[str], operator.add]
```

`total=False` means every key is optional, because the graph fills them in
progressively — `snapshot` does not exist before the analyse node runs. Nodes
therefore read with `state.get(...)`, which is honest: at any given node most of
this genuinely is not there yet.

Everything in state must survive being written to disk and read back. That is
why it holds data — Pydantic models, lists, strings — and never a live HTTP
client or an open file. Those are [dependencies](../../src/patchpilot/agent/deps.py),
passed in when the graph is built.

### 2. Nodes — functions that return only what changed

```python
def rank_issues_node(state: AgentState) -> AgentState:
    ranked = rank_issues(state.get("candidate_issues") or [])
    return AgentState(visited=["rank_issues"], ranked_issues=ranked)
```

A node receives the whole state and returns **just the keys it touched**.
LangGraph merges that in.

### 3. Reducers — the part that is easy to get wrong

By default, returning a key **replaces** its value.

That is wrong for anything that should accumulate. If two nodes each report an
error, replacement leaves only the second one. If each node appends its name to
a trail, replacement leaves a trail of length one.

So those fields declare a **reducer** — a function of (existing, incoming)
returning the merged value:

```python
visited: Annotated[list[str], operator.add]        # concatenate
usage:   Annotated[Usage, accumulate_usage]        # sum tokens and latency
```

Without the reducer on `usage`, the last node's token count would overwrite
everything before it and the cost of a run would be unknowable.

**Rule of thumb:** if the answer to "what if two nodes both write this?" is
"keep both", it needs a reducer.

### 4. Edges — fixed and conditional

```python
graph.add_edge("rank_issues", "select_issue")            # always

graph.add_conditional_edges(                              # it depends
    "analyze_repository",
    continue_or_halt,                    # a function returning a label
    {"continue": "discover_issues", "halt": END},
)
```

The routing function is the interesting part, because it is *small and pure*:

```python
def continue_or_halt(state) -> Literal["continue", "halt"]:
    return "halt" if state.get("halted") else "continue"
```

"When does the agent stop?" is now a function you can test with a dictionary,
separate from any node's logic. In the plain version it would be an early
`return` tangled into the middle of the work.

### 5. Compile, then invoke

```python
compiled = graph.compile(checkpointer=saver)
final = compiled.invoke(initial_state(run_id, "simonw/sqlite-utils"),
                        config={"configurable": {"thread_id": run_id}})
```

`thread_id` is LangGraph's name for "which run is this". Passing the same one
again **resumes** that run instead of starting a new one — see
[checkpointing and resume](checkpointing-and-resume.md).

## In PatchPilot

- [`agent/state.py`](../../src/patchpilot/agent/state.py) — state and reducers.
- [`agent/graph.py`](../../src/patchpilot/agent/graph.py) — assembly and routing.
- [`agent/nodes/`](../../src/patchpilot/agent/nodes/) — the nodes.
- [`agent/deps.py`](../../src/patchpilot/agent/deps.py) — the live objects that
  cannot live in state.

Today the graph is a straight line with halt exits:

```
START → analyze_repository ─┬→ discover_issues ─┬→ rank_issues → select_issue → END
                            └→ END              └→ END
                             (halted)            (halted)
```

## What goes wrong

**Forgetting a reducer.** The symptom is data quietly disappearing — one error
in the list when there were three. There is a test for it:
`test_visited_accumulates_rather_than_overwrites`.

**Putting a live object in state.** It will not serialise, and the failure
appears at checkpoint time rather than where you wrote it.

**Returning the whole state from a node.** It works, and it destroys the
"returns only what changed" property that makes merging predictable.

**Changing the state schema after checkpoints exist.** Old checkpoints were
written with the old shape. There is no migration story, which is a genuine cost
of this design — noted in [ADR-007](../adr/ADR-007-langgraph.md).

**Assuming keys exist.** With `total=False`, `state["snapshot"]` raises in any
node that runs before the analyser. Use `.get()`.

## Interview questions

**Q: What is state in an agent, and why does it need to be explicit?**

It is everything the run knows, as one serialisable value rather than local
variables. Explicit because three things require it: pausing for a human and
resuming in a different process, recovering from a crash without repeating
expensive work, and looping with accumulated knowledge. Local variables die when
the function returns; a state value can be written to disk and read back.

**Q: What is a reducer and when do you need one?**

A function that merges a node's output into existing state instead of replacing
it. You need one whenever the right answer to "two nodes both wrote this" is
"keep both" — an error list, a trail of visited nodes, accumulated token usage.
Without one on token usage, the last node's count overwrites the rest and you
cannot cost a run.

**Q: Why are conditional edges better than an `if` inside the node?**

Because routing becomes a small pure function, testable with a dictionary and
separate from the work the node does. It also makes control flow visible in the
graph definition rather than buried in a function body — which matters when
someone asks which paths can reach the node that touches git.

**Q: Why is the GitHub client not in the state?**

State is serialised to the checkpoint database and an open socket does not
survive that. Live objects are passed in when the graph is built and closed over
by the nodes. A useful side effect is that tests substitute a whole world — fake
model, mock transport, temp workspace — and the graph runs end to end offline.

## Official documentation

- [LangGraph — Low-level concepts](https://langchain-ai.github.io/langgraph/concepts/low_level/) — state, nodes, edges, reducers. The most useful single page.
- [LangGraph — Graph API](https://langchain-ai.github.io/langgraph/how-tos/graph-api/) — `add_node`, `add_conditional_edges`, compilation.
