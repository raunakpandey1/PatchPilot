"""Node: check the patch against policy, and assemble what a human needs.

Deterministic. No model reviews the model's work — that is a well-known way to
get confident approval of a bad change, because the reviewer shares the
generator's blind spots and, being the same weights, often its exact reasoning.

What this node does instead is mechanical: run the policy, scan the untrusted
inputs for injection signals, and assemble a single object a person can act on.
"""

from __future__ import annotations

from collections.abc import Callable

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.state import AgentState
from patchpilot.guardrails.injection import scan_many
from patchpilot.guardrails.policy import Decision, Policy
from patchpilot.logging import get_logger
from patchpilot.models import PatchReview

log = get_logger("node.review")

NodeFn = Callable[[AgentState], AgentState]


def make_review_patch_node(deps: AgentDeps) -> NodeFn:
    policy = deps.policy or Policy()

    def review_patch_node(state: AgentState) -> AgentState:
        patch = state.get("patch")
        snapshot = state.get("snapshot")
        validation = state.get("validation")

        if patch is None or snapshot is None:
            return AgentState(
                visited=["review_patch"], halted=True,
                halt_reason="no patch to review",
            )

        objections = policy.check_patch(patch, test_files=snapshot.test_files)
        decision = policy.evaluate_patch(patch, test_files=snapshot.test_files)

        # Scan the untrusted inputs that reached the model. Reported, not
        # blocking: the layers below this one are what actually protect, and a
        # false positive that halted a run would get the check switched off.
        documents: list[tuple[str, str]] = []
        if issue := state.get("selected_issue"):
            documents.append((f"issue #{issue.number}", issue.text))
        for scored in state.get("retrieved_chunks") or []:
            documents.append((scored.chunk.file_path, scored.chunk.text))
        signals = scan_many(documents)

        risks: list[str] = []
        if validation is not None and not validation.passed:
            risks.append("the test suite did not pass with this patch applied")
        if state.get("debug_attempts", 0) > 0:
            risks.append(f"took {state['debug_attempts']} repair attempt(s)")
        if plan := state.get("fix_plan"):
            risks.extend(plan.risks)

        review = PatchReview(
            policy_decision=str(decision),
            objections=tuple(str(v) for v in objections),
            injection_signals=tuple(str(s) for s in signals),
            risk_notes=tuple(risks),
        )

        log.info(
            "patch_reviewed",
            decision=str(decision), objections=len(objections),
            injection_signals=len(signals), risks=len(risks),
        )

        if decision is Decision.DENY:
            # Not offered to a human. Some things are not a judgement call, and
            # an override option is how they get overridden by someone tired.
            return AgentState(
                visited=["review_patch"], review=review, halted=True,
                halt_reason="policy denied this patch: " + "; ".join(review.objections[:3]),
            )

        return AgentState(visited=["review_patch"], review=review)

    return review_patch_node
