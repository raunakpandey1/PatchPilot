"""Node: repair a patch that failed its tests, and the routing that bounds it.

The loop
--------
::

    generate_patch → validate_patch ──passed──→ END
                          │
                        failed
                          │
                          ▼
                       repair ──→ validate_patch ──→ ...

Why the loop must be bounded, three ways
-----------------------------------------
Nothing in a language model produces "I give up". Asked to try again, it will
always try again. Three separate limits are therefore enforced in code:

1. **A maximum attempt count.** The blunt bound. Always reached eventually.
2. **Loop detection.** If two consecutive attempts produce the *same* failure,
   the model is not making progress and further attempts will not either. This
   matters more than the count: it stops five wasted calls rather than letting
   them run out.
3. **The call budget** in :mod:`patchpilot.llm.cache`, which bounds spend across
   the whole run regardless of what any loop does.

The first is obvious, the second is what actually saves quota, and the third is
what saves you when the first two have a bug.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.nodes.fixing import EditList
from patchpilot.agent.prompts.debugging import (
    REPAIR_SYSTEM,
    build_repair_prompt,
    failure_signature,
)
from patchpilot.agent.prompts.fixing import extract_regions
from patchpilot.agent.state import AgentState
from patchpilot.llm.base import LLMError, user
from patchpilot.logging import get_logger
from patchpilot.tools.patching import apply_patch

log = get_logger("node.debugging")

NodeFn = Callable[[AgentState], AgentState]

Route = Literal["done", "repair", "give_up"]


def route_after_validation(state: AgentState) -> Route:
    """Decide whether to stop, repair, or give up.

    A small pure function over state, for the same reason as
    ``continue_or_halt``: "when does the agent stop trying?" should be testable
    with a dictionary rather than by running a whole graph and watching.
    """
    if state.get("halted"):
        return "give_up"

    validation = state.get("validation")
    if validation is None:
        return "give_up"

    if validation.passed:
        return "done"

    attempts = state.get("debug_attempts", 0)
    if attempts >= state.get("max_debug_attempts", 3):
        return "give_up"

    # No progress: this attempt failed in exactly the same way as the last one.
    # Trying again spends a call to learn the same thing a third time.
    history = state.get("failure_signatures") or []
    if len(history) >= 2 and history[-1] == history[-2]:
        return "give_up"

    return "repair"


def make_repair_patch_node(deps: AgentDeps) -> NodeFn:
    def repair_patch_node(state: AgentState) -> AgentState:
        root_cause = state.get("root_cause")
        plan = state.get("fix_plan")
        patch = state.get("patch")
        validation = state.get("validation")
        snapshot = state.get("snapshot")

        # Checked individually rather than with `all(...)`, which does not
        # narrow the optional types for the type checker.
        if (
            root_cause is None
            or plan is None
            or patch is None
            or validation is None
            or snapshot is None
        ):
            return AgentState(
                visited=["repair_patch"], halted=True,
                halt_reason="cannot repair without a previous patch and its validation",
            )

        attempt = state.get("debug_attempts", 0) + 1
        max_attempts = state.get("max_debug_attempts", 3)

        regions = extract_regions(Path(snapshot.local_path), root_cause, plan)
        if not regions:
            return AgentState(
                visited=["repair_patch"], debug_attempts=attempt, halted=True,
                halt_reason="no file content available to repair against",
            )

        prompt = build_repair_prompt(
            root_cause, plan, patch, validation, regions,
            attempt=attempt, max_attempts=max_attempts,
            previous_failures=state.get("failure_signatures") or [],
        )

        try:
            result = deps.llm.complete_structured(
                [user(prompt)], EditList, system=REPAIR_SYSTEM, temperature=0.0,
                max_output_tokens=deps.settings.llm_max_output_tokens,
            )
        except LLMError as exc:
            log.warning("repair_failed", attempt=attempt, error=str(exc))
            return AgentState(
                visited=["repair_patch"], debug_attempts=attempt,
                errors=[f"repair attempt {attempt} failed: {exc}"],
                halted=True, halt_reason=str(exc),
            )

        edits = result.value.edits
        if not edits:
            # The model declining after seeing real failure output is a
            # meaningful signal — usually that the diagnosis was wrong. Better
            # than forcing a change that cannot work.
            return AgentState(
                visited=["repair_patch"], debug_attempts=attempt, usage=result.usage,
                halted=True,
                halt_reason=f"repair produced no edits: {result.value.explanation[:200]}",
            )

        applied = apply_patch(
            deps.workspace, Path(snapshot.local_path), edits,
            attempt_name=f"{state['run_id']}_attempt{attempt}",
            explanation=result.value.explanation,
        )

        log.info(
            "patch_repaired",
            attempt=attempt, edits=len(edits), applied=applied.applied,
            added=applied.patch.lines_added, removed=applied.patch.lines_removed,
            tokens=result.usage.total_tokens,
        )

        return AgentState(
            visited=["repair_patch"],
            debug_attempts=attempt,
            patch=applied.patch,
            working_copy=str(applied.workspace_path),
            patch_errors=[applied.failure_report()] if not applied.applied else [],
            usage=result.usage,
        )

    return repair_patch_node


def make_record_failure_node(deps: AgentDeps) -> NodeFn:
    """Record a fingerprint of this attempt's failure, for loop detection.

    A separate node rather than part of validation because it is bookkeeping
    about the *loop*, not about the patch — and keeping it separate means the
    validation node stays a pure "run the checks and report" step.
    """

    def record_failure_node(state: AgentState) -> AgentState:
        validation = state.get("validation")
        if validation is None or validation.passed:
            return AgentState(visited=["record_failure"])

        signature = failure_signature(validation)
        history = state.get("failure_signatures") or []
        repeated = bool(history) and history[-1] == signature

        log.info(
            "failure_recorded",
            signature=signature[:120],
            attempt=state.get("debug_attempts", 0),
            repeated=repeated,
        )
        return AgentState(visited=["record_failure"], failure_signatures=[signature])

    return record_failure_node
