"""Node: fetch and analyse the repository.

Deterministic. No LLM. It wraps Phase 1 — see
:mod:`patchpilot.analysis.repository` — and puts the result into agent state.
"""

from __future__ import annotations

from collections.abc import Callable

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.state import AgentState
from patchpilot.analysis.repository import analyze_repository
from patchpilot.errors import GitError, PatchPilotError
from patchpilot.logging import get_logger
from patchpilot.tools.git import GitRepository, clone

log = get_logger("node.repository")

# A node receives the whole state and returns only the keys it changed.
# ``AgentState`` is ``total=False``, so a partial value is a valid one — which
# is exactly what LangGraph expects back.
NodeFn = Callable[[AgentState], AgentState]


def make_analyze_repository_node(deps: AgentDeps) -> NodeFn:
    def analyze_repository_node(state: AgentState) -> AgentState:
        full_name = state["repository_full_name"]

        try:
            repository = deps.github.get_repository(full_name)
        except PatchPilotError as exc:
            # A missing or private repository is not a crash — it is an outcome.
            # The graph halts and says why, rather than raising through five
            # frames of framework.
            log.warning("repository_unavailable", repo=full_name, error=str(exc))
            return AgentState(
                visited=["analyze_repository"],
                errors=[f"could not fetch {full_name}: {exc}"],
                halted=True,
                halt_reason=str(exc),
            )

        destination = deps.workspace.repo_dir(full_name)
        try:
            if (destination / ".git").exists():
                # Reuse an existing clone. Cloning again would spend minutes and
                # bandwidth to produce the same bytes.
                local = GitRepository(destination)
                log.info("clone_reused", repo=full_name, path=str(destination))
            else:
                local = clone(
                    repository.clone_url,
                    destination,
                    strategy=deps.clone_strategy,
                    branch=repository.default_branch,
                    timeout_s=deps.settings.clone_timeout_s,
                    max_size_mb=deps.settings.max_repo_size_mb,
                )
        except GitError as exc:
            log.warning("clone_failed", repo=full_name, error=str(exc))
            return AgentState(
                visited=["analyze_repository"],
                repository=repository,
                errors=[f"clone failed: {exc}"],
                halted=True,
                halt_reason=str(exc),
            )

        snapshot = analyze_repository(repository, local)

        if not snapshot.is_testable:
            # Refuse work we cannot verify. A patch with no way to run tests
            # against it is not a contribution, it is a guess with a diff.
            log.warning("repository_not_testable", repo=full_name)
            return AgentState(
                visited=["analyze_repository"],
                repository=repository,
                snapshot=snapshot,
                halted=True,
                halt_reason=(
                    "no test command could be discovered, so a patch could not be validated"
                ),
            )

        return AgentState(
            visited=["analyze_repository"],
            repository=repository,
            snapshot=snapshot,
        )

    return analyze_repository_node
