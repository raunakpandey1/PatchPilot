"""Node: stop and wait for a human.

How pausing actually works
--------------------------
``interrupt()`` raises a special exception that LangGraph catches. The graph
stops, the current state is written to the checkpointer, and ``invoke()``
returns with the run incomplete.

The process can then exit entirely. Hours later, a *different* process calls
``invoke`` again with the same ``thread_id`` and a ``Command(resume=...)``
carrying the decision. LangGraph loads the state, re-enters this node, and
``interrupt()`` returns the resumed value instead of raising.

That is the whole mechanism, and it is the reason this project is a graph rather
than a loop: a function's local variables cannot survive the process exiting.

What is deliberately *not* here
--------------------------------
No default. No timeout that approves. No "approve if the tests passed". The
absence of a decision is not a decision, and every convenience that turns
silence into approval removes the only layer a human is actually in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.types import interrupt

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.state import AgentState
from patchpilot.logging import get_logger
from patchpilot.models import ApprovalStatus

log = get_logger("node.approval")

NodeFn = Callable[[AgentState], AgentState]


def build_approval_request(state: AgentState) -> dict[str, Any]:
    """Everything the reviewer sees.

    Assembled as data rather than a formatted string so the CLI, the web UI and
    a test can each render it their own way — and so a test can assert the diff
    is actually present rather than that some text was produced.
    """
    issue = state.get("selected_issue")
    patch = state.get("patch")
    validation = state.get("validation")
    review = state.get("review")
    root_cause = state.get("root_cause")
    plan = state.get("fix_plan")

    return {
        "run_id": state.get("run_id", ""),
        "repository": state.get("repository_full_name", ""),
        "issue": {
            "number": issue.number if issue else None,
            "title": issue.title if issue else "",
            "url": issue.html_url if issue else "",
        },
        "root_cause": root_cause.summary_for_human() if root_cause else "",
        "plan": plan.summary_for_human() if plan else "",
        "diff": patch.diff if patch else "",
        "files_changed": list(patch.files_changed) if patch else [],
        "lines_added": patch.lines_added if patch else 0,
        "lines_removed": patch.lines_removed if patch else 0,
        "validation": validation.summary_for_human() if validation else "not run",
        "tests_passed": bool(validation and validation.passed),
        "review": review.summary_for_human() if review else "",
        "repair_attempts": state.get("debug_attempts", 0),
        "tokens_used": usage.total_tokens if (usage := state.get("usage")) else 0,
        "options": ["approve", "reject", "request_changes"],
    }


def make_human_approval_node(deps: AgentDeps) -> NodeFn:
    def human_approval_node(state: AgentState) -> AgentState:
        if deps.auto_approve:
            # Only for tests and benchmarks, where no human exists and nothing
            # is pushed. Never a production path — see ADR-018.
            log.warning("approval_bypassed", run_id=state.get("run_id"))
            return AgentState(
                visited=["human_approval"],
                approval_status=ApprovalStatus.APPROVED,
                approval_note="auto-approved (no human in the loop)",
            )

        request = build_approval_request(state)
        log.info("awaiting_approval", run_id=state.get("run_id"), files=len(request["files_changed"]))

        # Execution stops here. The state is checkpointed, and this returns only
        # when a later invocation resumes the same thread with a decision.
        response = interrupt(request)

        decision, note = _parse(response)
        log.info("approval_received", run_id=state.get("run_id"), decision=str(decision))

        return AgentState(
            visited=["human_approval"],
            approval_status=decision,
            approval_note=note,
            halted=decision is not ApprovalStatus.APPROVED,
            halt_reason=(
                "" if decision is ApprovalStatus.APPROVED else f"human decision: {decision}"
            ),
        )

    return human_approval_node


def _parse(response: Any) -> tuple[ApprovalStatus, str]:
    """Interpret whatever the resuming caller supplied.

    Anything unrecognised is a **rejection**, never an approval. A malformed
    resume value must not be able to become consent — that is the one direction
    in which being permissive is unacceptable.
    """
    if isinstance(response, dict):
        raw = str(response.get("decision", "")).lower().strip()
        note = str(response.get("note", ""))
    else:
        raw = str(response).lower().strip()
        note = ""

    if raw in ("approve", "approved", "yes", "y"):
        return ApprovalStatus.APPROVED, note
    if raw in ("request_changes", "changes", "revise"):
        return ApprovalStatus.CHANGES_REQUESTED, note
    if raw in ("reject", "rejected", "no", "n"):
        return ApprovalStatus.REJECTED, note

    return ApprovalStatus.REJECTED, f"unrecognised response {raw!r}; treated as rejection"
