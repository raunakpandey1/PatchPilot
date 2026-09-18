"""Nodes: retrieve context for the selected issue, then find the root cause.

This is the first place an LLM makes a decision. Everything before it — repo
analysis, issue ranking, retrieval — is deterministic, which is deliberate: the
model is used for judgement over ambiguous evidence, never for facts already
written down.

The two nodes are separate on purpose. Retrieval is measurable on its own
(Recall@K, MRR — see the Phase 3 benchmark), and separating it means a bad root
cause can be attributed: *was the right code even in the prompt?* Fused into one
node, that question is unanswerable, and every failure looks like "the model is
bad at this".
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.prompts.investigation import (
    PROMPT_VERSION,
    SYSTEM,
    build_investigation_prompt,
)
from patchpilot.agent.state import AgentState
from patchpilot.llm.base import LLMError, user
from patchpilot.logging import get_logger
from patchpilot.models import RootCause
from patchpilot.rag.store import ScoredChunk

log = get_logger("node.investigation")

NodeFn = Callable[[AgentState], AgentState]


def make_retrieve_context_node(deps: AgentDeps) -> NodeFn:
    """Retrieve the code most relevant to the selected issue."""

    def retrieve_context_node(state: AgentState) -> AgentState:
        issue = state.get("selected_issue")
        snapshot = state.get("snapshot")
        if issue is None or snapshot is None:
            return AgentState(
                visited=["retrieve_context"],
                halted=True,
                halt_reason="no issue selected to investigate",
            )

        if deps.retriever is None:
            return AgentState(
                visited=["retrieve_context"],
                halted=True,
                halt_reason="no retriever configured; the repository must be indexed first",
            )

        chunks = deps.retriever.retrieve(
            issue.text,
            limit=deps.retrieval_limit,
            mode=deps.retrieval_mode,
            repository=snapshot.repository.full_name,
            exclude_tests=True,
        )

        if not chunks:
            # An empty retrieval is not a model problem and must not be handed
            # to the model as though it were. Nothing downstream can recover
            # from having no code to reason about.
            return AgentState(
                visited=["retrieve_context"],
                halted=True,
                halt_reason="retrieval returned no code for this issue",
            )

        log.info(
            "context_retrieved",
            issue=issue.number,
            chunks=len(chunks),
            files=len({c.chunk.file_path for c in chunks}),
            mode=deps.retrieval_mode,
        )
        return AgentState(visited=["retrieve_context"], retrieved_chunks=chunks)

    return retrieve_context_node


def make_root_cause_node(deps: AgentDeps) -> NodeFn:
    """Ask the model for a root cause, constrained to the `RootCause` schema."""

    def root_cause_node(state: AgentState) -> AgentState:
        issue = state.get("selected_issue")
        snapshot = state.get("snapshot")
        chunks = state.get("retrieved_chunks") or []

        if issue is None or snapshot is None or not chunks:
            return AgentState(
                visited=["analyze_root_cause"],
                halted=True,
                halt_reason="missing issue, snapshot or retrieved context",
            )

        prompt = build_investigation_prompt(issue, snapshot, chunks)

        try:
            result = deps.llm.complete_structured(
                [user(prompt)],
                RootCause,
                system=SYSTEM,
                temperature=0.0,
                max_output_tokens=deps.settings.llm_max_output_tokens,
            )
        except LLMError as exc:
            log.warning("root_cause_failed", issue=issue.number, error=str(exc))
            return AgentState(
                visited=["analyze_root_cause"],
                errors=[f"root cause analysis failed: {exc}"],
                halted=True,
                halt_reason=str(exc),
            )

        root_cause = result.value
        hallucinated = _citations_outside_context(root_cause, chunks)

        log.info(
            "root_cause_found",
            issue=issue.number,
            confidence=str(root_cause.confidence),
            primary_file=root_cause.primary_file,
            evidence=len(root_cause.evidence),
            hallucinated_citations=len(hallucinated),
            prompt_version=PROMPT_VERSION,
            **{"tokens": result.usage.total_tokens},
        )

        if hallucinated:
            # The model cited a file it was never shown. That is a fabrication,
            # and it is checkable in code rather than trusted — the prompt asks
            # the model not to do it, and this verifies that it did not.
            return AgentState(
                visited=["analyze_root_cause"],
                root_cause=root_cause,
                usage=result.usage,
                errors=[
                    "root cause cited files not present in the retrieved context: "
                    + ", ".join(sorted(hallucinated))
                ],
                halted=True,
                halt_reason="root cause cited code that was never retrieved",
            )

        if not root_cause.is_actionable:
            # Low confidence, or no evidence. Refusing here is the point: a
            # patch built on an imagined cause looks like progress and is not.
            return AgentState(
                visited=["analyze_root_cause"],
                root_cause=root_cause,
                usage=result.usage,
                halted=True,
                halt_reason=(
                    f"root cause not actionable (confidence={root_cause.confidence}, "
                    f"evidence={len(root_cause.evidence)})"
                ),
            )

        return AgentState(
            visited=["analyze_root_cause"],
            root_cause=root_cause,
            usage=result.usage,
        )

    return root_cause_node


def _citations_outside_context(
    root_cause: RootCause, chunks: Sequence[ScoredChunk]
) -> set[str]:
    """Files the model cited that were never in the prompt.

    A cheap, deterministic hallucination check. It cannot tell whether the
    *reasoning* is right, but it can tell whether the model invented a file —
    and that is the failure that most reliably wrecks the next step.
    """
    available = {c.chunk.file_path for c in chunks}
    cited = set(root_cause.cited_files) | {root_cause.primary_file}
    return {path for path in cited if path and path not in available}
