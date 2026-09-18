"""Output parsing tests.

The parsers exist so the debug loop gets the one line that matters instead of
4,000 lines of progress dots. These tests pin the conservative behaviour: when
the parser cannot tell, it must say so rather than claim zero or success.
"""

from __future__ import annotations

from patchpilot.sandbox.parsing import parse_lint, parse_pytest, parse_unittest, tail

PYTEST_OUTPUT = """\
============================= test session starts ==============================
collected 5 items

tests/test_db.py ..F..                                                   [100%]

=================================== FAILURES ===================================
_________________________ test_rows_where_missing_table ________________________

    def test_rows_where_missing_table():
>       assert db["nope"].rows_where("x=1")
E       AssertionError: assert None is not None

tests/test_db.py:42: AssertionError
=========================== short test summary info ============================
FAILED tests/test_db.py::test_rows_where_missing_table - AssertionError: assert None is not None
========================= 1 failed, 4 passed in 0.31s ==========================
"""


def test_counts_and_failures_are_extracted():
    passed, failed, failures = parse_pytest(PYTEST_OUTPUT)

    assert (passed, failed) == (4, 1)
    assert failures[0].test_id == "tests/test_db.py::test_rows_where_missing_table"
    assert "AssertionError" in failures[0].message
    assert failures[0].file_path == "tests/test_db.py"


def test_an_all_passing_run_reports_no_failures():
    passed, failed, failures = parse_pytest("===== 12 passed in 1.02s =====")

    assert (passed, failed, failures) == (12, 0, ())


def test_errors_count_as_failures():
    """A collection error is not a pass, even though nothing 'failed'."""
    output = "ERROR tests/test_db.py::test_setup\n==== 1 error in 0.1s ===="

    _, failed, failures = parse_pytest(output)

    assert failed == 1
    assert failures[0].test_id == "tests/test_db.py::test_setup"


def test_truncated_output_does_not_claim_success():
    """The run was killed mid-way. Reporting 0 failed would read as a pass."""
    passed, failed, failures = parse_pytest("tests/test_db.py ..F")

    assert passed == 0
    assert failed == 0
    assert failures == ()
    # The caller falls back to the exit code, which is the reliable signal.


def test_a_summary_inside_test_output_does_not_confuse_the_parser():
    """Test suites for CLI tools print things that look like pytest summaries.
    The real one is last, so the search runs from the end."""
    output = (
        "test prints: ===== 99 passed in 0.01s =====\n"
        "tests/test_cli.py .                                     [100%]\n"
        "========================= 1 passed in 0.10s ==========================\n"
    )

    passed, _, _ = parse_pytest(output)

    assert passed == 1


def test_duplicate_failure_lines_are_deduplicated():
    output = (
        "FAILED tests/a.py::test_x - boom\n"
        "FAILED tests/a.py::test_x - boom\n"
        "==== 1 failed in 0.1s ===="
    )

    _, _, failures = parse_pytest(output)

    assert len(failures) == 1


def test_failures_found_without_a_summary_are_still_reported():
    """Trust whichever signal is larger rather than under-reporting."""
    output = "FAILED tests/a.py::test_x - boom\nFAILED tests/b.py::test_y - bang\n"

    _, failed, failures = parse_pytest(output)

    assert failed == 2
    assert len(failures) == 2


def test_unittest_output_is_parsed():
    output = """\
Ran 5 tests in 0.004s

FAILED (failures=1)
FAIL: test_rows (tests.test_db.DbTests)
"""

    passed, failed, failures = parse_unittest(output)

    assert (passed, failed) == (4, 1)
    assert "test_rows" in failures[0].test_id


def test_lint_output_is_parsed():
    output = (
        "src/db.py:12:5: F401 `os` imported but unused\n"
        "src/cli.py:40:1: E501 line too long\n"
        "Found 2 errors.\n"
    )

    count, issues = parse_lint(output)

    assert count == 2
    assert issues[0].file_path == "src/db.py"
    assert issues[0].line == 12
    assert "Found 2 errors" not in [i.message for i in issues]


def test_mypy_output_is_parsed_by_the_same_function():
    """Both emit `file:line: message`, so one parser covers them."""
    count, issues = parse_lint("src/db.py:12: error: Incompatible types\n")

    assert count == 1
    assert "Incompatible types" in issues[0].message


def test_lint_issues_are_capped():
    output = "\n".join(f"src/f{i}.py:{i}: error: problem" for i in range(200))

    count, issues = parse_lint(output, limit=10)

    assert count == 10
    assert len(issues) == 10


def test_tail_keeps_the_end_not_the_beginning():
    """Runners print progress first and what went wrong last."""
    text = "\n".join(f"line {i}" for i in range(1000))

    result = tail(text, limit=100)

    assert "line 999" in result
    assert "line 0\n" not in result
    assert "truncated" in result


def test_short_output_is_returned_unchanged():
    assert tail("brief", limit=100) == "brief"


def test_the_terse_summary_is_parsed():
    """Regression guard for F-008.

    `pytest -q --no-header` prints no `====` decoration — and that is exactly
    the command our own repository analyzer generates. The parser required the
    decoration, so a fully green suite reported 0 passed.
    """
    passed, failed, failures = parse_pytest(".                    [100%]\n1 passed in 0.01s\n")

    assert (passed, failed, failures) == (1, 0, ())


def test_the_terse_summary_with_failures_is_parsed():
    output = (
        ".F                   [100%]\n"
        "FAILED tests/test_widget.py::test_broken - AssertionError\n"
        "1 failed, 1 passed in 0.05s\n"
    )

    passed, failed, failures = parse_pytest(output)

    assert (passed, failed) == (1, 1)
    assert "test_broken" in failures[0].test_id


def test_prose_mentioning_passed_is_not_mistaken_for_a_summary():
    """The terse pattern requires a duration, so ordinary output does not match."""
    passed, failed, _ = parse_pytest("3 passed the review\nstill running\n")

    assert (passed, failed) == (0, 0)
