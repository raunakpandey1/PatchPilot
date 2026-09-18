"""Debug loop tests.

The loop is the part that can burn a day's quota if its bound has a bug, so
these focus on termination: it stops when the tests pass, it stops at the
attempt limit, and — the one that actually saves calls — it stops early when an
attempt fails in exactly the same way as the last.
"""

from __future__ import annotations

import pytest

from patchpilot.agent.nodes.debugging import route_after_validation
from patchpilot.agent.prompts.debugging import (
    REPAIR_SYSTEM,
    build_repair_prompt,
    failure_signature,
)
from patchpilot.agent.state import AgentState
from patchpilot.models import (
    CheckOutcome,
    CheckResult,
    Confidence,
    Evidence,
    FixPlan,
    Patch,
    RootCause,
    TestFailure,
    ValidationResult,
)

PLAN = FixPlan(
    approach="remove the guard", files_to_change=("src/db.py",),
    steps=("delete it",), test_strategy="pytest",
)
ROOT_CAUSE = RootCause(
    summary="early return", explanation="x", primary_file="src/db.py",
    evidence=(Evidence(file_path="src/db.py", start_line=1, end_line=5, why_relevant="y"),),
    confidence=Confidence.HIGH,
)


def failing(*test_ids: str) -> ValidationResult:
    return ValidationResult(
        checks=(
            CheckResult(
                name="tests", command=("pytest",), outcome=CheckOutcome.FAILED, exit_code=1,
                failed_count=len(test_ids),
                failures=tuple(TestFailure(test_id=t, message="boom") for t in test_ids),
            ),
        )
    )


def passing() -> ValidationResult:
    return ValidationResult(
        checks=(
            CheckResult(
                name="tests", command=("pytest",), outcome=CheckOutcome.PASSED,
                exit_code=0, passed_count=10,
            ),
        )
    )


# --- routing: when does the loop stop? --------------------------------------


def test_passing_tests_end_the_loop():
    assert route_after_validation(AgentState(validation=passing())) == "done"


def test_a_failure_within_the_budget_triggers_a_repair():
    state = AgentState(validation=failing("t::a"), debug_attempts=0, max_debug_attempts=3)

    assert route_after_validation(state) == "repair"


def test_the_attempt_limit_stops_the_loop():
    """The blunt bound. Nothing in a model produces 'I give up' on its own."""
    state = AgentState(validation=failing("t::a"), debug_attempts=3, max_debug_attempts=3)

    assert route_after_validation(state) == "give_up"


def test_two_identical_failures_stop_the_loop_early():
    """The bound that actually saves quota.

    If an attempt fails in exactly the same way as the last, the model is not
    making progress and the remaining attempts will learn nothing new.
    """
    state = AgentState(
        validation=failing("t::a"),
        debug_attempts=1,
        max_debug_attempts=5,
        failure_signatures=["tests:t::a", "tests:t::a"],
    )

    assert route_after_validation(state) == "give_up"


def test_different_failures_allow_the_loop_to_continue():
    """Progress, even if not success — the failure changed, so the last attempt
    did something."""
    state = AgentState(
        validation=failing("t::b"),
        debug_attempts=1,
        max_debug_attempts=5,
        failure_signatures=["tests:t::a", "tests:t::b"],
    )

    assert route_after_validation(state) == "repair"


def test_a_sandbox_error_does_not_loop_forever():
    state = AgentState(
        validation=ValidationResult(sandbox_error="daemon down"),
        debug_attempts=0, max_debug_attempts=3,
        failure_signatures=["sandbox:daemon down", "sandbox:daemon down"],
    )

    assert route_after_validation(state) == "give_up"


def test_missing_validation_gives_up_rather_than_looping():
    assert route_after_validation(AgentState(debug_attempts=0)) == "give_up"


def test_a_halted_run_is_not_retried():
    state = AgentState(halted=True, validation=failing("t::a"), debug_attempts=0)

    assert route_after_validation(state) == "give_up"


# --- failure fingerprinting -------------------------------------------------


def test_the_signature_ignores_message_detail():
    """Messages carry varying data — addresses, temp paths, timestamps — which
    would make two identical failures look different and defeat the check."""
    first = ValidationResult(checks=(
        CheckResult(name="tests", command=("p",), outcome=CheckOutcome.FAILED,
                    failures=(TestFailure(test_id="t::a", message="boom at 0x7f8a"),)),
    ))
    second = ValidationResult(checks=(
        CheckResult(name="tests", command=("p",), outcome=CheckOutcome.FAILED,
                    failures=(TestFailure(test_id="t::a", message="boom at 0x9c21"),)),
    ))

    assert failure_signature(first) == failure_signature(second)


def test_the_signature_ignores_failure_order():
    assert failure_signature(failing("t::a", "t::b")) == failure_signature(failing("t::b", "t::a"))


def test_different_failing_tests_produce_different_signatures():
    assert failure_signature(failing("t::a")) != failure_signature(failing("t::b"))


def test_a_sandbox_error_has_its_own_signature():
    assert failure_signature(ValidationResult(sandbox_error="x")).startswith("sandbox:")


def test_a_failure_with_no_parseable_tests_still_fingerprints():
    """A lint failure has no test ids, and must still be comparable."""
    result = ValidationResult(checks=(
        CheckResult(name="lint", command=("ruff",), outcome=CheckOutcome.FAILED, exit_code=1),
    ))

    assert failure_signature(result).startswith("checks:")


# --- the repair prompt ------------------------------------------------------


def test_the_failure_comes_before_the_diagnosis():
    """Test output is the only input that is not an opinion, so it leads."""
    prompt = build_repair_prompt(
        ROOT_CAUSE, PLAN, Patch(edits=(), diff="- old\n+ new"), failing("t::a"),
        [("src/db.py", 1, "code here")], attempt=1, max_attempts=3,
    )

    assert prompt.index("What went wrong") < prompt.index("Original diagnosis")


def test_the_previous_attempt_is_shown():
    """Without it the model regenerates something very close to what just
    failed."""
    prompt = build_repair_prompt(
        ROOT_CAUSE, PLAN, Patch(edits=(), diff="- removed_line"), failing("t::a"),
        [("src/db.py", 1, "code")], attempt=2, max_attempts=3,
    )

    assert "removed_line" in prompt
    assert "Attempt 2 of 3" in prompt


def test_already_tried_approaches_are_listed():
    prompt = build_repair_prompt(
        ROOT_CAUSE, PLAN, Patch(edits=()), failing("t::a"),
        [("src/db.py", 1, "code")], attempt=3, max_attempts=3,
        previous_failures=["tests:t::a", "tests:t::b"],
    )

    assert "already tried" in prompt.lower()
    assert "Do not repeat these" in prompt


def test_the_diagnosis_is_marked_as_possibly_wrong():
    """The test suite is better evidence than the earlier model call."""
    prompt = build_repair_prompt(
        ROOT_CAUSE, PLAN, Patch(edits=()), failing("t::a"),
        [("src/db.py", 1, "code")], attempt=1, max_attempts=3,
    )

    assert "may be wrong" in prompt


def normalized(text: str) -> str:
    """Collapse whitespace so assertions survive line wrapping.

    The prompt is wrapped for readability, so `return no\n  edits` is a single
    instruction split across lines. Asserting on raw text would make every
    reflow a test failure — testing the formatting rather than the content.
    """
    return " ".join(text.split())


def test_deleting_tests_is_forbidden_in_the_system_prompt():
    """The cheapest way to make a failing test pass is to delete it, which is
    why it is ruled out explicitly here and enforced in Phase 8's policy."""
    text = normalized(REPAIR_SYSTEM)

    assert "NEVER delete, skip, weaken or `xfail` a test" in text
    assert "not a fix, it is a lie about one" in text


def test_the_model_may_declare_the_diagnosis_wrong():
    assert "return no edits" in normalized(REPAIR_SYSTEM)


def test_untrusted_content_is_fenced_in_the_repair_prompt():
    prompt = build_repair_prompt(
        ROOT_CAUSE, PLAN, Patch(edits=()), failing("t::a"),
        [("src/db.py", 1, "code")], attempt=1, max_attempts=3,
    )

    assert "UNTRUSTED_CONTENT" in prompt


@pytest.mark.parametrize("attempts,limit,expected", [
    (0, 3, "repair"), (1, 3, "repair"), (2, 3, "repair"), (3, 3, "give_up"), (4, 3, "give_up"),
])
def test_the_bound_is_inclusive(attempts, limit, expected):
    state = AgentState(validation=failing("t::a"), debug_attempts=attempts,
                       max_debug_attempts=limit)

    assert route_after_validation(state) == expected
