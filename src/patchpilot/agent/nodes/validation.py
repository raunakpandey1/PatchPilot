"""Node: run the repository's own checks against the patched copy.

This is the node that turns "the patch looks right" into "the tests pass with it
applied" — the difference between a demo and a result.

Deterministic. No model involved: the test suite decides, not an opinion about
the test suite.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.state import AgentState
from patchpilot.logging import get_logger
from patchpilot.models import ValidationResult
from patchpilot.sandbox.docker_runner import SandboxError

log = get_logger("node.validation")

NodeFn = Callable[[AgentState], AgentState]


def make_validate_patch_node(deps: AgentDeps) -> NodeFn:
    def validate_patch_node(state: AgentState) -> AgentState:
        snapshot = state.get("snapshot")
        working_copy = state.get("working_copy")

        if snapshot is None or not working_copy:
            return AgentState(
                visited=["validate_patch"], halted=True,
                halt_reason="no patched working copy to validate",
            )

        if deps.sandbox is None:
            # Refusing is the point. An unvalidated patch must never be
            # presented as though it had been checked — "we could not tell" and
            # "it works" are different answers.
            return AgentState(
                visited=["validate_patch"], halted=True,
                halt_reason="no sandbox configured; a patch cannot be validated",
            )

        # Edits that never applied cannot be meaningfully tested — the working
        # copy does not contain the intended change. Report it as a validation
        # failure so the debug loop repairs the patch rather than the code.
        if state.get("patch_errors"):
            failed = ValidationResult(
                sandbox_error="the patch did not apply:\n" + "\n".join(state["patch_errors"])
            )
            return AgentState(visited=["validate_patch"], validation=failed)

        try:
            image = deps.sandbox.prepare_image(
                snapshot, tag=f"patchpilot/{_image_tag(snapshot.repository.full_name)}"
            )
            result = deps.sandbox.run_checks(
                image,
                Path(working_copy),
                snapshot,
                include_lint=deps.validate_lint,
                include_typecheck=deps.validate_types,
            )
        except SandboxError as exc:
            log.warning("sandbox_failed", error=str(exc))
            result = ValidationResult(sandbox_error=str(exc))

        log.info(
            "patch_validated",
            passed=result.passed,
            checks=len(result.checks),
            failures=len(result.failures),
            sandbox_error=result.sandbox_error is not None,
        )
        return AgentState(visited=["validate_patch"], validation=result)

    return validate_patch_node


def _image_tag(full_name: str) -> str:
    """`owner/name` is not a legal Docker tag component on its own."""
    return full_name.replace("/", "__").lower() + ":latest"
