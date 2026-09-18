"""The agent graph.

What a graph is, and why not a plain loop
----------------------------------------
This phase's graph is a straight line::

    START → analyze_repository → discover_issues → rank_issues → select_issue → END

A ``for`` loop over four function calls would do the same thing today. The graph
earns its place because of three properties this project needs, none of which a
loop provides:

**1. Stopping early is data, not control flow.** Any node can return
``halted=True`` with a reason. A conditional edge after each step routes to END.
In a loop that is an early ``return`` or an exception, and the "why" has to be
threaded back by hand.

**2. Checkpointing.** With a checkpointer attached, the state after every node is
written to SQLite. A crash in the sandbox does not mean re-cloning and
re-indexing — it means resuming from the last checkpoint. On a free-tier budget
that is a cost control, not an elegance argument. It is also what makes Phase 9's
human approval possible at all: the run stops, the process exits, and a *later*
invocation resumes the same state.

**3. Cycles.** Phase 7 adds ``code_generator → test → debugger → code_generator``
with a bound on iterations. Expressed as a conditional edge, "when do we go
back?" is a small testable function, separate from "how do we fix code?". In a
loop those two concerns braid together in one function nobody can test in
pieces.

The honest cost: another abstraction to debug through, framework stack traces,
and a state schema that is awkward to migrate once checkpoints exist on disk.
See ADR-007.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.nodes.debugging import (
    make_record_failure_node,
    make_repair_patch_node,
    route_after_validation,
)
from patchpilot.agent.nodes.fixing import (
    make_generate_patch_node,
    make_plan_fix_node,
)
from patchpilot.agent.nodes.investigation import (
    make_retrieve_context_node,
    make_root_cause_node,
)
from patchpilot.agent.nodes.issues import (
    make_discover_issues_node,
    make_rank_issues_node,
    make_select_issue_node,
)
from patchpilot.agent.nodes.repository import make_analyze_repository_node
from patchpilot.agent.nodes.validation import make_validate_patch_node
from patchpilot.agent.state import AgentState, initial_state
from patchpilot.logging import get_logger

log = get_logger("graph")


def continue_or_halt(state: AgentState) -> Literal["continue", "halt"]:
    """The routing function.

    Deliberately tiny and pure: it reads state and returns a label, nothing
    else. That makes "when does the agent stop?" a function you can test with a
    dictionary, rather than behaviour you can only observe by running the whole
    graph.
    """
    return "halt" if state.get("halted") else "continue"


def build_graph(deps: AgentDeps, *, checkpointer: Any | None = None) -> Any:
    """Assemble and compile the graph.

    Nodes close over ``deps``, so the graph is built per set of dependencies.
    Tests build one with a fake LLM and a mock GitHub client and run it end to
    end offline.
    """
    graph = StateGraph(AgentState)

    # The `type: ignore` is upstream typing friction, not a bug here.
    # LangGraph's `add_node` overloads infer their node type parameter from a
    # directly-defined function; they cannot infer it from a value whose type is
    # a `Callable[...]` alias — which is what a node *factory* returns. Verified
    # by reducing it to a six-line file with no PatchPilot code in it:
    # a plain `def node(state: S) -> S` is accepted, `make() -> Callable[[S], S]`
    # is not. The runtime behaviour is identical; only the stubs disagree.
    graph.add_node("analyze_repository", make_analyze_repository_node(deps))  # type: ignore[call-overload]
    graph.add_node("discover_issues", make_discover_issues_node(deps))  # type: ignore[call-overload]
    graph.add_node("rank_issues", make_rank_issues_node(deps))  # type: ignore[call-overload]
    graph.add_node("select_issue", make_select_issue_node(deps))  # type: ignore[call-overload]
    graph.add_node("retrieve_context", make_retrieve_context_node(deps))  # type: ignore[call-overload]
    graph.add_node("analyze_root_cause", make_root_cause_node(deps))  # type: ignore[call-overload]
    graph.add_node("plan_fix", make_plan_fix_node(deps))  # type: ignore[call-overload]
    graph.add_node("generate_patch", make_generate_patch_node(deps))  # type: ignore[call-overload]
    graph.add_node("validate_patch", make_validate_patch_node(deps))  # type: ignore[call-overload]
    graph.add_node("record_failure", make_record_failure_node(deps))  # type: ignore[call-overload]
    graph.add_node("repair_patch", make_repair_patch_node(deps))  # type: ignore[call-overload]

    graph.add_edge(START, "analyze_repository")

    # After every node that can fail, the same question: keep going, or stop and
    # report? Written once as a function and reused, rather than four bespoke
    # early-return branches.
    for source, following in (
        ("analyze_repository", "discover_issues"),
        ("discover_issues", "rank_issues"),
        ("select_issue", "retrieve_context"),
        ("retrieve_context", "analyze_root_cause"),
        ("analyze_root_cause", "plan_fix"),
        ("plan_fix", "generate_patch"),
        ("generate_patch", "validate_patch"),
    ):
        graph.add_conditional_edges(
            source, continue_or_halt, {"continue": following, "halt": END}
        )

    graph.add_edge("rank_issues", "select_issue")
    # The debug cycle. Validation always records its outcome first, then the
    # routing function decides: stop, try again, or give up. Expressed as a
    # conditional edge so "when does the agent stop trying?" is one small pure
    # function rather than a loop condition tangled into a node.
    graph.add_edge("validate_patch", "record_failure")
    graph.add_conditional_edges(
        "record_failure",
        route_after_validation,
        {"done": END, "repair": "repair_patch", "give_up": END},
    )
    graph.add_conditional_edges(
        "repair_patch", continue_or_halt, {"continue": "validate_patch", "halt": END}
    )

    return graph.compile(checkpointer=checkpointer)


@contextmanager
def sqlite_checkpointer(path: Path) -> Iterator[SqliteSaver]:
    """A checkpointer backed by a SQLite file.

    SQLite rather than Postgres because this runs on a laptop with 8 GB of RAM
    and a checkpointer is not the place to spend a gigabyte on a database
    server. The interface is the same one Postgres implements, so moving is a
    constructor change (ADR-009).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        yield saver


def memory_checkpointer() -> InMemorySaver:
    """An in-process checkpointer, for tests.

    Exercises the same resume machinery without touching the filesystem.
    """
    return InMemorySaver()


def run(
    deps: AgentDeps,
    repository_full_name: str,
    *,
    run_id: str,
    checkpointer: Any | None = None,
) -> AgentState:
    """Execute the graph once and return the final state.

    ``thread_id`` is LangGraph's name for "which run is this". Passing the same
    one again resumes that run rather than starting a new one — the mechanism
    Phase 9 uses to continue after a human approves.
    """
    compiled = build_graph(deps, checkpointer=checkpointer)
    config = {"configurable": {"thread_id": run_id}}

    log.info("run_started", run_id=run_id, repo=repository_full_name)
    final: AgentState = compiled.invoke(
        initial_state(run_id, repository_full_name), config=config
    )
    selected = final.get("selected_issue")
    log.info(
        "run_finished",
        run_id=run_id,
        halted=final.get("halted", False),
        visited=final.get("visited", []),
        selected=selected.number if selected else None,
    )
    return final
