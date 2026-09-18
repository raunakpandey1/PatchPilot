"""Nodes: plan a fix, then write it.

Two model calls, separated on purpose — see
:mod:`patchpilot.agent.prompts.fixing` for why. Both are checked in code
afterwards, because the prompt asking for something is not evidence that it
happened.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.prompts.fixing import (
    PATCH_SYSTEM,
    PLAN_SYSTEM,
    build_patch_prompt,
    build_plan_prompt,
    extract_regions,
)
from patchpilot.agent.state import AgentState
from patchpilot.llm.base import LLMError, user
from patchpilot.logging import get_logger
from patchpilot.models import CodeEdit, FixPlan
from patchpilot.tools.patching import apply_patch

log = get_logger("node.fixing")

NodeFn = Callable[[AgentState], AgentState]


class EditList(BaseModel):
    """Wrapper schema.

    Structured output needs an object at the root, so a bare list of edits is
    not expressible. ``explanation`` also gives the model somewhere to say "I
    could not do this safely" while returning zero edits — which is a better
    outcome than a guessed edit.
    """

    edits: tuple[CodeEdit, ...] = ()
    explanation: str = ""


def make_plan_fix_node(deps: AgentDeps) -> NodeFn:
    def plan_fix_node(state: AgentState) -> AgentState:
        issue = state.get("selected_issue")
        root_cause = state.get("root_cause")
        snapshot = state.get("snapshot")

        if issue is None or root_cause is None or snapshot is None:
            return AgentState(
                visited=["plan_fix"], halted=True,
                halt_reason="cannot plan a fix without a diagnosed root cause",
            )

        try:
            result = deps.llm.complete_structured(
                [user(build_plan_prompt(issue, root_cause, snapshot))],
                FixPlan,
                system=PLAN_SYSTEM,
                temperature=0.0,
                max_output_tokens=deps.settings.llm_max_output_tokens,
            )
        except LLMError as exc:
            log.warning("fix_plan_failed", issue=issue.number, error=str(exc))
            return AgentState(
                visited=["plan_fix"], errors=[f"fix planning failed: {exc}"],
                halted=True, halt_reason=str(exc),
            )

        plan = result.value

        # A plan naming files the diagnosis never cited means the model went
        # beyond its evidence. Checked here rather than trusted, and it is a
        # cheap check precisely because planning is a separate step.
        allowed = set(root_cause.cited_files) | {root_cause.primary_file}
        unsupported = [f for f in plan.files_to_change if f not in allowed]

        log.info(
            "fix_planned",
            issue=issue.number, files=len(plan.files_to_change),
            steps=len(plan.steps), unsupported_files=len(unsupported),
            tokens=result.usage.total_tokens,
        )

        if not plan.files_to_change:
            return AgentState(
                visited=["plan_fix"], fix_plan=plan, usage=result.usage,
                halted=True, halt_reason="the plan proposes no file changes",
            )

        if unsupported:
            return AgentState(
                visited=["plan_fix"], fix_plan=plan, usage=result.usage,
                errors=[
                    "plan targets files absent from the diagnosis: "
                    + ", ".join(sorted(unsupported))
                ],
                halted=True,
                halt_reason="fix plan went beyond the evidence",
            )

        return AgentState(visited=["plan_fix"], fix_plan=plan, usage=result.usage)

    return plan_fix_node


def make_generate_patch_node(deps: AgentDeps) -> NodeFn:
    def generate_patch_node(state: AgentState) -> AgentState:
        issue = state.get("selected_issue")
        root_cause = state.get("root_cause")
        plan = state.get("fix_plan")
        snapshot = state.get("snapshot")

        if issue is None or root_cause is None or plan is None or snapshot is None:
            return AgentState(
                visited=["generate_patch"], halted=True,
                halt_reason="cannot generate a patch without issue, root cause and plan",
            )

        regions = extract_regions(Path(snapshot.local_path), root_cause, plan)
        if not regions:
            return AgentState(
                visited=["generate_patch"], halted=True,
                halt_reason="no file content available for the planned changes",
            )

        try:
            result = deps.llm.complete_structured(
                [user(build_patch_prompt(issue, root_cause, plan, regions))],
                EditList,
                system=PATCH_SYSTEM,
                temperature=0.0,
                max_output_tokens=deps.settings.llm_max_output_tokens,
            )
        except LLMError as exc:
            log.warning("patch_generation_failed", issue=issue.number, error=str(exc))
            return AgentState(
                visited=["generate_patch"], errors=[f"patch generation failed: {exc}"],
                halted=True, halt_reason=str(exc),
            )

        edits = result.value.edits
        if not edits:
            # The model declining is a legitimate answer, and a better one than
            # a guessed edit. Its explanation is kept for the human.
            return AgentState(
                visited=["generate_patch"], usage=result.usage,
                halted=True,
                halt_reason=f"model produced no edits: {result.value.explanation[:200]}",
            )

        attempt = state.get("debug_attempts", 0)
        applied = apply_patch(
            deps.workspace,
            Path(snapshot.local_path),
            edits,
            attempt_name=f"{state['run_id']}_attempt{attempt}",
            explanation=result.value.explanation,
        )

        log.info(
            "patch_generated",
            issue=issue.number, edits=len(edits), applied=applied.applied,
            files=len(applied.patch.files_changed),
            added=applied.patch.lines_added, removed=applied.patch.lines_removed,
            tokens=result.usage.total_tokens,
        )

        if not applied.applied:
            # Not fatal — this is exactly what Phase 7's debug loop repairs, and
            # the failure report is written to be actionable.
            return AgentState(
                visited=["generate_patch"],
                patch=applied.patch,
                working_copy=str(applied.workspace_path),
                patch_errors=[applied.failure_report()],
                usage=result.usage,
            )

        return AgentState(
            visited=["generate_patch"],
            patch=applied.patch,
            working_copy=str(applied.workspace_path),
            usage=result.usage,
        )

    return generate_patch_node
