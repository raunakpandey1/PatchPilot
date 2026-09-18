"""Benchmark harness tests.

Testing the measurement, not the thing measured. A benchmark with a bug reports
numbers that are wrong in a confident, specific way — which this project has
already been bitten by twice (F-005, F-006).
"""

from __future__ import annotations

import pytest

from patchpilot.agent.state import AgentState
from patchpilot.evaluation.benchmark import (
    IssueResult,
    Outcome,
    classify,
    summarize_results,
)
from patchpilot.models import (
    CheckOutcome,
    CheckResult,
    CodeEdit,
    Patch,
    PatchReview,
    ValidationResult,
)


def result(outcome: Outcome, **kwargs) -> IssueResult:
    return IssueResult(issue_number=kwargs.pop("n", 1), outcome=outcome, **kwargs)


def report_for(*results: IssueResult):
    return summarize_results(results, repository="a/b", model="m")


# --- rates and denominators -------------------------------------------------


def test_infrastructure_errors_are_excluded_from_the_denominator():
    """A Docker daemon that was down says nothing about the agent. Counting it
    as a failure would make the number a measure of the laptop."""
    report = report_for(
        result(Outcome.FIXED, n=1), result(Outcome.ERROR, n=2),
    )

    assert report.attempted == 1
    assert report.success_rate == 1.0


def test_first_attempt_and_eventual_rates_differ_by_what_the_loop_is_worth():
    report = report_for(
        result(Outcome.FIXED, n=1, repair_attempts=0),
        result(Outcome.FIXED, n=2, repair_attempts=2),
        result(Outcome.PATCH_FAILED_TESTS, n=3, repair_attempts=3),
    )

    assert report.first_attempt_rate == pytest.approx(1 / 3)
    assert report.success_rate == pytest.approx(2 / 3)


def test_issue_to_patch_rate_separates_could_not_attempt_from_failed():
    """Different problems. An agent that never produces a patch needs work on
    retrieval or diagnosis; one whose patches fail needs work on generation."""
    report = report_for(
        result(Outcome.FIXED, n=1),
        result(Outcome.PATCH_FAILED_TESTS, n=2),
        result(Outcome.NO_ROOT_CAUSE, n=3),
    )

    assert report.issue_to_patch_rate == pytest.approx(2 / 3)
    assert report.success_rate == pytest.approx(1 / 3)


def test_tokens_per_success_is_reported_alongside_median():
    """Cost per attempt flatters a system that fails cheaply. What you pay for
    is a fix."""
    report = report_for(
        result(Outcome.FIXED, n=1, tokens=10_000),
        result(Outcome.NO_PATCH, n=2, tokens=1_000),
        result(Outcome.NO_PATCH, n=3, tokens=1_000),
    )

    assert report.median_tokens == 1_000
    assert report.tokens_per_success == 12_000


def test_no_successes_does_not_divide_by_zero():
    assert report_for(result(Outcome.NO_PATCH, tokens=500)).tokens_per_success == float("inf")


def test_an_empty_benchmark_reports_zero_rather_than_crashing():
    report = summarize_results([], repository="a/b", model="m")

    assert report.attempted == 0
    assert report.success_rate == 0.0


def test_a_small_sample_is_flagged_in_the_report():
    """Five issues cannot distinguish a 40% from a 60% success rate, and a
    report that does not say so invites over-reading."""
    text = report_for(*[result(Outcome.FIXED, n=i) for i in range(5)]).report()

    assert "too few" in text
    assert "directional" in text


def test_a_large_sample_is_not_flagged():
    text = report_for(*[result(Outcome.FIXED, n=i) for i in range(25)]).report()

    assert "too few" not in text


def test_the_model_is_recorded_in_the_report():
    """The fallback chain means a run can be answered by a different model than
    the one configured. A rate without the model is not comparable."""
    assert "gemini-3.8-flash" in summarize_results(
        [result(Outcome.FIXED)], repository="a/b", model="gemini-3.8-flash"
    ).report()


# --- classification ---------------------------------------------------------


def passing_validation() -> ValidationResult:
    return ValidationResult(checks=(
        CheckResult(name="tests", command=("p",), outcome=CheckOutcome.PASSED, exit_code=0),
    ))


def failing_validation() -> ValidationResult:
    return ValidationResult(checks=(
        CheckResult(name="tests", command=("p",), outcome=CheckOutcome.FAILED, exit_code=1),
    ))


def test_passing_tests_classify_as_fixed():
    assert classify(AgentState(validation=passing_validation())) is Outcome.FIXED


def test_a_policy_block_is_distinguished_from_a_test_failure():
    state = AgentState(
        validation=failing_validation(),
        review=PatchReview(policy_decision="deny", objections=("weakens a test",)),
    )

    assert classify(state) is Outcome.POLICY_BLOCKED


def test_the_earliest_failure_is_the_one_reported():
    """A run that never retrieved context also has no patch. Reporting NO_PATCH
    would point at the wrong stage."""
    state = AgentState(halt_reason="retrieval returned no code for this issue")

    assert classify(state) is Outcome.NO_CONTEXT


@pytest.mark.parametrize(("reason", "expected"), [
    ("retrieval returned no code for this issue", Outcome.NO_CONTEXT),
    ("root cause not actionable (confidence=low)", Outcome.NO_ROOT_CAUSE),
    ("root cause cited code that was never retrieved", Outcome.NO_ROOT_CAUSE),
    ("model produced no edits: the excerpt is insufficient", Outcome.NO_PATCH),
    ("fix plan went beyond the evidence", Outcome.NO_PATCH),
])
def test_halt_reasons_map_to_specific_outcomes(reason, expected):
    assert classify(AgentState(halt_reason=reason)) is expected


def test_a_patch_that_did_not_apply_is_its_own_outcome():
    state = AgentState(patch_errors=["old_text was not found"], patch=Patch(edits=()))

    assert classify(state) is Outcome.PATCH_DID_NOT_APPLY


def test_a_sandbox_failure_is_an_infrastructure_error_not_an_agent_failure():
    state = AgentState(validation=ValidationResult(sandbox_error="daemon down"))

    assert classify(state) is Outcome.ERROR


def test_a_patch_that_failed_its_tests():
    state = AgentState(
        validation=failing_validation(),
        patch=Patch(edits=(CodeEdit(file_path="a.py", old_text="x", new_text="y", reason="r"),)),
    )

    assert classify(state) is Outcome.PATCH_FAILED_TESTS
