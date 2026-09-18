"""The end-to-end benchmark: does the agent actually fix issues?

What is being measured, and against what
-----------------------------------------
Retrieval already has its own benchmark (:mod:`patchpilot.evaluation.retrieval`)
because a stage you cannot measure separately is a stage you cannot debug. This
one measures the whole pipeline on the only question that matters:

    given a real issue, does the agent produce a patch that passes the
    repository's own test suite?

Ground truth is the repository's tests, not a human's opinion of the diff. That
is the strongest available oracle: it is the same standard a maintainer would
apply, and it cannot be argued with.

The metrics, and what each is for
----------------------------------
* **issue-to-patch rate** — produced a patch at all. Separates "could not
  attempt" from "attempted and failed", which are different problems.
* **first-attempt success** — passed the tests with no repair. The honest
  measure of the generator alone.
* **eventual success** — passed within the repair budget. The measure of the
  *loop*, and the difference between the two is what the loop is worth.
* **safety blocks** — patches the policy refused. A number that should be
  non-zero: if nothing is ever blocked, the policy is decoration.
* **cost** — tokens and calls per issue, and per *success*. Cost per attempt
  flatters a system that fails cheaply.

Why every result records the model
-----------------------------------
The fallback chain means a run can be answered by a different model than the one
configured. A success rate without the model that produced it is not comparable
with anything, including itself a week later.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from patchpilot.agent.state import AgentState
from patchpilot.logging import get_logger

log = get_logger("eval.benchmark")


class Outcome(StrEnum):
    """Why a run ended. Distinguishing these is the point.

    "Failed" is not one thing: an agent that cannot retrieve the code has a
    different problem from one that writes a patch failing the tests, and
    collapsing them into a single success rate hides which to work on.
    """

    FIXED = "fixed"                     # patch produced and tests passed
    PATCH_FAILED_TESTS = "patch_failed_tests"
    NO_PATCH = "no_patch"               # model declined or produced no edits
    PATCH_DID_NOT_APPLY = "patch_did_not_apply"
    NO_ROOT_CAUSE = "no_root_cause"     # investigation was not actionable
    NO_CONTEXT = "no_context"           # retrieval found nothing
    POLICY_BLOCKED = "policy_blocked"
    ERROR = "error"                     # infrastructure, not the agent's fault


class IssueResult(BaseModel):
    """One issue's outcome."""

    model_config = ConfigDict(frozen=True)

    issue_number: int
    outcome: Outcome
    repair_attempts: int = 0
    tokens: int = 0
    model_calls: int = 0
    duration_s: float = 0.0
    files_changed: int = 0
    lines_changed: int = 0
    model: str = ""
    note: str = ""

    @property
    def produced_a_patch(self) -> bool:
        return self.outcome in (Outcome.FIXED, Outcome.PATCH_FAILED_TESTS)

    @property
    def succeeded(self) -> bool:
        return self.outcome is Outcome.FIXED

    @property
    def first_attempt_success(self) -> bool:
        return self.succeeded and self.repair_attempts == 0


class BenchmarkReport(BaseModel):
    """Aggregate results, with the denominators stated."""

    model_config = ConfigDict(frozen=True)

    results: tuple[IssueResult, ...]
    model: str = ""
    repository: str = ""

    @property
    def attempted(self) -> int:
        """Issues where the agent got far enough to try.

        Infrastructure errors are excluded from the denominator: a Docker daemon
        that was down says nothing about the agent, and counting it as a failure
        would make the number a measure of the laptop.
        """
        return sum(1 for r in self.results if r.outcome is not Outcome.ERROR)

    @property
    def fixed(self) -> int:
        return sum(1 for r in self.results if r.succeeded)

    @property
    def issue_to_patch_rate(self) -> float:
        return self._rate(sum(1 for r in self.results if r.produced_a_patch))

    @property
    def success_rate(self) -> float:
        return self._rate(self.fixed)

    @property
    def first_attempt_rate(self) -> float:
        return self._rate(sum(1 for r in self.results if r.first_attempt_success))

    @property
    def policy_block_rate(self) -> float:
        return self._rate(sum(1 for r in self.results if r.outcome is Outcome.POLICY_BLOCKED))

    @property
    def median_tokens(self) -> float:
        values = [r.tokens for r in self.results if r.tokens]
        return statistics.median(values) if values else 0.0

    @property
    def tokens_per_success(self) -> float:
        """Total spend divided by successes.

        Reported alongside median-per-issue because cost per *attempt* flatters
        a system that fails cheaply — the thing you actually pay for is a fix.
        """
        return sum(r.tokens for r in self.results) / self.fixed if self.fixed else float("inf")

    @property
    def outcome_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[str(result.outcome)] = counts.get(str(result.outcome), 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def _rate(self, count: int) -> float:
        """The exact rate, unrounded.

        Rounding here would mix computation with presentation: `report()`
        formats to one decimal place, and a caller comparing two rates wants the
        real values. Rounding in the property made an exact-equality test fail
        for a reason that had nothing to do with the metric.
        """
        return count / self.attempted if self.attempted else 0.0

    def report(self) -> str:
        lines = [
            f"Benchmark: {self.repository} · model {self.model or 'unknown'}",
            f"  issues run: {len(self.results)} ({self.attempted} attempted, "
            f"{len(self.results) - self.attempted} infrastructure errors excluded)",
            "",
            f"  issue-to-patch rate : {self.issue_to_patch_rate:.1%}",
            f"  eventual fix rate   : {self.success_rate:.1%}  ({self.fixed}/{self.attempted})",
            f"  first-attempt rate  : {self.first_attempt_rate:.1%}",
            f"  policy block rate   : {self.policy_block_rate:.1%}",
            "",
            f"  median tokens/issue : {self.median_tokens:,.0f}",
            f"  tokens per success  : {self.tokens_per_success:,.0f}",
            "",
            "  outcomes:",
        ]
        lines += [f"    {name:<24} {count}" for name, count in self.outcome_counts.items()]

        if self.attempted and self.attempted < 20:
            lines += [
                "",
                f"  NOTE: {self.attempted} issues is too few to distinguish rates that differ",
                "  by less than roughly 20 percentage points. Treat these as directional.",
            ]
        return "\n".join(lines)


def classify(state: AgentState) -> Outcome:
    """Map a finished agent state to an outcome.

    Ordered most-specific first, because several conditions can be true at once
    and the *earliest* failure is the informative one — a run that never
    retrieved context also has no patch, and reporting NO_PATCH would point at
    the wrong stage.
    """
    validation = state.get("validation")
    if validation is not None and validation.passed:
        return Outcome.FIXED

    review = state.get("review")
    if review is not None and review.is_blocked:
        return Outcome.POLICY_BLOCKED

    reason = (state.get("halt_reason") or "").lower()

    if "no code" in reason or "retrieval returned no" in reason:
        return Outcome.NO_CONTEXT
    if "not actionable" in reason or "never retrieved" in reason:
        return Outcome.NO_ROOT_CAUSE
    if "no edits" in reason or "no file changes" in reason or "beyond the evidence" in reason:
        return Outcome.NO_PATCH
    if state.get("patch_errors"):
        return Outcome.PATCH_DID_NOT_APPLY
    if validation is not None and validation.sandbox_error:
        return Outcome.ERROR
    if state.get("patch") is not None:
        return Outcome.PATCH_FAILED_TESTS
    if reason:
        return Outcome.ERROR
    return Outcome.NO_PATCH


def summarize_results(
    results: Sequence[IssueResult], *, repository: str, model: str
) -> BenchmarkReport:
    report = BenchmarkReport(results=tuple(results), repository=repository, model=model)
    log.info(
        "benchmark_complete",
        issues=len(results), attempted=report.attempted, fixed=report.fixed,
        success_rate=round(report.success_rate, 4), model=model,
    )
    return report
