"""Nodes: discover, rank and select an issue.

All three are deterministic. Ranking in particular is arithmetic over observable
properties — see :mod:`patchpilot.agent.ranking` for why that is not an LLM.
"""

from __future__ import annotations

from collections.abc import Callable

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.ranking import rank_issues
from patchpilot.agent.state import AgentState
from patchpilot.errors import PatchPilotError
from patchpilot.logging import get_logger

log = get_logger("node.issues")

NodeFn = Callable[[AgentState], AgentState]


def make_discover_issues_node(deps: AgentDeps) -> NodeFn:
    def discover_issues_node(state: AgentState) -> AgentState:
        full_name = state["repository_full_name"]
        try:
            issues = deps.github.list_issues(full_name, state="open", limit=deps.max_issues)
        except PatchPilotError as exc:
            log.warning("issue_discovery_failed", repo=full_name, error=str(exc))
            return AgentState(
                visited=["discover_issues"],
                errors=[f"could not list issues: {exc}"],
                halted=True,
                halt_reason=str(exc),
            )

        if not issues:
            return AgentState(
                visited=["discover_issues"],
                candidate_issues=[],
                halted=True,
                halt_reason="the repository has no open issues",
            )

        return AgentState(visited=["discover_issues"], candidate_issues=issues)

    return discover_issues_node


def make_rank_issues_node(deps: AgentDeps) -> NodeFn:
    def rank_issues_node(state: AgentState) -> AgentState:
        candidates = state.get("candidate_issues") or []
        ranked = rank_issues(candidates)

        counts: dict[str, int] = {}
        for r in ranked:
            counts[r.verdict] = counts.get(r.verdict, 0) + 1

        log.info("issues_ranked", total=len(ranked), **counts)
        return AgentState(visited=["rank_issues"], ranked_issues=ranked)

    return rank_issues_node


def make_select_issue_node(deps: AgentDeps) -> NodeFn:
    def select_issue_node(state: AgentState) -> AgentState:
        ranked = state.get("ranked_issues") or []

        # An explicitly requested issue bypasses the ranking entirely. The
        # ranker decides what is *worth* attempting; a caller naming an issue
        # has already made that decision, and overriding them would be
        # surprising — a benchmark pins issues precisely so results stay
        # comparable across ranker changes.
        if (requested := state.get("requested_issue")) is not None:
            match = next((r for r in ranked if r.issue.number == requested), None)
            if match is None:
                return AgentState(
                    visited=["select_issue"], halted=True,
                    halt_reason=f"issue #{requested} is not among the open issues",
                )
            log.info(
                "issue_selected", number=requested, requested=True,
                score=round(match.score, 3), verdict=match.verdict,
            )
            return AgentState(
                visited=["select_issue"],
                selected_issue=match.issue,
                selection_reason=match.explain(),
            )

        actionable = [r for r in ranked if r.is_actionable]

        if not actionable:
            best = ranked[0] if ranked else None
            reason = (
                f"no issue scored above the 'attempt' threshold; "
                f"best was #{best.issue.number} at {best.score:.2f}"
                if best
                else "no issues to rank"
            )
            log.info("no_actionable_issue", reason=reason)
            return AgentState(visited=["select_issue"], halted=True, halt_reason=reason)

        chosen = actionable[0]
        log.info(
            "issue_selected",
            number=chosen.issue.number,
            score=round(chosen.score, 3),
            title=chosen.issue.title[:80],
        )
        return AgentState(
            visited=["select_issue"],
            selected_issue=chosen.issue,
            selection_reason=chosen.explain(),
        )

    return select_issue_node
