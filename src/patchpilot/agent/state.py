"""The agent's state.

What "state" means here
-----------------------
An ordinary Python function keeps its working data in local variables. When the
function returns, they are gone. That is fine until you need to do one of three
things PatchPilot must do:

1. **Pause and resume across processes.** A human approves a patch minutes or
   hours later, possibly from a second CLI invocation. The agent must be able to
   stop, be written to disk, and pick up exactly where it left off.
2. **Recover from a crash** without redoing expensive work. Re-cloning,
   re-indexing and re-investigating costs free-tier quota we do not have.
3. **Loop.** The debug loop revisits earlier steps with accumulated knowledge.

All three require the working data to be an explicit, serialisable *value*
rather than local variables. That value is this.

How LangGraph uses it
---------------------
Each node receives the state and returns a dict of the keys it changed.
LangGraph merges that in. By default, returning a key **replaces** it.

For keys that should accumulate rather than be overwritten — errors, the trail
of visited nodes, token usage — the field is annotated with a **reducer**: a
function taking (existing, incoming) and returning the merged value. Without
reducers, two nodes both reporting an error would leave only the second one.

Everything here must survive being serialised to the checkpoint database and
read back, which is why the values are Pydantic models and plain types rather
than open file handles or clients.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from patchpilot.agent.ranking import RankedIssue
from patchpilot.llm.base import Usage
from patchpilot.models import Issue, Repository, RepositorySnapshot, RootCause
from patchpilot.rag.store import ScoredChunk


def accumulate_usage(existing: Usage, incoming: Usage) -> Usage:
    """Reducer that sums token usage across every node in a run.

    Without this, the last node's usage would overwrite everything before it and
    the cost of a run would be unknowable.
    """
    return existing + incoming


class AgentState(TypedDict, total=False):
    """Everything the agent knows.

    ``total=False`` means every key is optional, because the graph fills them in
    progressively — ``snapshot`` does not exist until the analyze node has run.
    Nodes must therefore read defensively (``state.get("snapshot")``), which is
    honest: at any given node, most of this is genuinely not there yet.
    """

    # --- identity ----------------------------------------------------------
    run_id: str
    """Correlates logs, traces and checkpoints for one run. Bound to the logging
    context so every event in the run carries it (see docs/concepts/structured-logging.md)."""

    repository_full_name: str
    """The input: "owner/name"."""

    # --- filled by the analyze node ---------------------------------------
    repository: Repository | None
    snapshot: RepositorySnapshot | None

    # --- filled by the discovery and ranking nodes ------------------------
    candidate_issues: list[Issue]
    ranked_issues: list[RankedIssue]
    selected_issue: Issue | None
    selection_reason: str

    # --- filled by the investigation nodes ---------------------------------
    retrieved_chunks: list[ScoredChunk]
    """The code excerpts put in front of the model. Kept in state so a failed
    run can be asked the diagnostic question that matters: was the right code
    even retrieved? Without it, every failure looks like 'the model is bad'."""

    root_cause: RootCause | None

    # --- accumulated across every node ------------------------------------
    usage: Annotated[Usage, accumulate_usage]
    """Total tokens and latency. Summed, not replaced."""

    visited: Annotated[list[str], operator.add]
    """Ordered list of node names. The agent's trajectory — what it actually did,
    which is the first thing you want when a run behaves oddly."""

    errors: Annotated[list[str], operator.add]
    """Non-fatal problems. Appended, never overwritten: a run can hit several and
    they all matter when explaining the outcome."""

    # --- control ------------------------------------------------------------
    halted: bool
    """Set when the graph stopped early on purpose — no actionable issue, for
    instance. Distinct from crashing, and reported differently."""

    halt_reason: str


def initial_state(run_id: str, repository_full_name: str) -> AgentState:
    """A fresh state.

    Collections are initialised here rather than defaulted in the nodes so that
    a node can always append without first checking whether the key exists.
    """
    return AgentState(
        run_id=run_id,
        repository_full_name=repository_full_name,
        repository=None,
        snapshot=None,
        candidate_issues=[],
        ranked_issues=[],
        selected_issue=None,
        selection_reason="",
        retrieved_chunks=[],
        root_cause=None,
        usage=Usage(),
        visited=[],
        errors=[],
        halted=False,
        halt_reason="",
    )


def summarize(state: AgentState) -> str:
    """One-paragraph description of where a run got to. Used by the CLI and logs."""
    parts = [f"run {state.get('run_id', '?')} · {state.get('repository_full_name', '?')}"]

    if snapshot := state.get("snapshot"):
        parts.append(f"snapshot @ {snapshot.head_sha[:8]} ({snapshot.total_files} files)")
    if ranked := state.get("ranked_issues"):
        actionable = sum(1 for r in ranked if r.is_actionable)
        parts.append(f"{len(ranked)} issues ranked, {actionable} actionable")
    if issue := state.get("selected_issue"):
        parts.append(f"selected #{issue.number}: {issue.title}")
    if chunks := state.get("retrieved_chunks"):
        files = len({c.chunk.file_path for c in chunks})
        parts.append(f"retrieved {len(chunks)} chunks from {files} files")
    if root_cause := state.get("root_cause"):
        parts.append(root_cause.summary_for_human())
    if state.get("halted"):
        parts.append(f"HALTED: {state.get('halt_reason', '')}")
    if errors := state.get("errors"):
        parts.append(f"{len(errors)} error(s)")

    usage = state.get("usage") or Usage()
    parts.append(f"{usage.total_tokens} tokens")
    parts.append(f"path: {' → '.join(state.get('visited', [])) or 'none'}")
    return "\n  ".join(parts)
