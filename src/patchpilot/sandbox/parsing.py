"""Turning a test runner's output into structured failures.

Why parse at all
----------------
The debug loop in Phase 7 has to repair a patch from the failure. Handing a
model 4,000 lines of pytest output is expensive and counterproductive: the one
line that matters is buried, and most of the tokens are progress dots and
tracebacks from library internals.

Parsing extracts the parts a repair actually needs — which test, which file,
which assertion — and lets everything else be dropped.

Why parse *conservatively*
--------------------------
Output formats change between versions and configurations. A parser that fails
to find a count must not claim zero; a parser that cannot identify a failure
must not claim success. Every function here degrades to "I could not tell" and
leaves the caller to fall back on the exit code, which is the one signal that is
always reliable.

This is the same discipline as the rest of the project: an honest "unknown"
beats a confident wrong answer, because the wrong answer propagates.
"""

from __future__ import annotations

import re

from patchpilot.models import TestFailure

# "FAILED tests/test_db.py::test_rows_where - AssertionError: assert None"
# "ERROR tests/test_db.py::test_setup"
PYTEST_FAILURE = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<test_id>\S+?)(?:\s+-\s+(?P<message>.*))?$", re.MULTILINE
)

# "=== 3 failed, 10 passed, 1 skipped in 2.34s ==="
PYTEST_COUNT = re.compile(r"(?P<count>\d+)\s+(?P<label>passed|failed|error|errors|skipped)")

# The *terse* summary, printed when pytest runs with `-q --no-header`:
#   "1 passed in 0.01s"
#   "1 failed, 4 passed in 0.31s"
# No `===` decoration at all. This is the form our own analyzer's generated
# command produces, and missing it meant reporting 0 passed for a green suite —
# see docs/failures.md F-008.
PYTEST_TERSE_SUMMARY = re.compile(
    r"^\s*\d+\s+(?:passed|failed|error|errors|skipped)\b.*\bin\s+[\d.]+\s*s",
    re.IGNORECASE,
)

# "tests/test_db.py:42: AssertionError"
PYTEST_LOCATION = re.compile(r"^(?P<file>[\w./\\-]+\.py):(?P<line>\d+):", re.MULTILINE)

# unittest: "FAIL: test_rows (tests.test_db.DbTests)"
UNITTEST_FAILURE = re.compile(r"^(?:FAIL|ERROR):\s+(?P<test_id>\S+.*)$", re.MULTILINE)
UNITTEST_COUNT = re.compile(r"^Ran (?P<count>\d+) tests?", re.MULTILINE)

# Both ruff and mypy emit `file:line: message`, e.g.
#   src/db.py:12:5: F401 `os` imported but unused          (ruff)
#   src/db.py:12: error: Incompatible types                (mypy)
#
# Note the examples are indented rather than written as `# mypy: ...` — a
# comment beginning with that prefix is read by mypy as an inline configuration
# directive, and it rejected the file with "Unrecognized option".
LINT_ISSUE = re.compile(
    r"^(?P<file>[\w./\\-]+):(?P<line>\d+)(?::\d+)?:\s*(?P<message>.+)$", re.MULTILINE
)


def parse_pytest(output: str) -> tuple[int, int, tuple[TestFailure, ...]]:
    """Return (passed, failed, failures) from pytest output.

    Counts come from the summary line, which pytest prints even with ``-q``.
    If it is absent — the run was killed, or output was truncated — the counts
    are 0 and the caller falls back to the exit code.
    """
    passed = failed = 0
    if summary := _last_summary_line(output):
        for match in PYTEST_COUNT.finditer(summary):
            count = int(match.group("count"))
            label = match.group("label")
            if label == "passed":
                passed = count
            elif label in ("failed", "error", "errors"):
                failed += count

    failures: list[TestFailure] = []
    seen: set[str] = set()
    for match in PYTEST_FAILURE.finditer(output):
        test_id = match.group("test_id")
        if test_id in seen:
            continue
        seen.add(test_id)

        file_path, line = _location_from_test_id(test_id)
        failures.append(
            TestFailure(
                test_id=test_id,
                message=(match.group("message") or "").strip(),
                file_path=file_path,
                line=line,
            )
        )

    # A run can fail without a parseable summary (a collection error, for
    # instance). Trust whichever signal is larger rather than under-reporting.
    failed = max(failed, len(failures))
    return passed, failed, tuple(failures)


def parse_unittest(output: str) -> tuple[int, int, tuple[TestFailure, ...]]:
    """Return (passed, failed, failures) from unittest output."""
    total = int(match.group("count")) if (match := UNITTEST_COUNT.search(output)) else 0

    failures = tuple(
        TestFailure(test_id=match.group("test_id").strip())
        for match in UNITTEST_FAILURE.finditer(output)
    )
    failed = len(failures)
    return max(0, total - failed), failed, failures


def parse_lint(output: str, *, limit: int = 50) -> tuple[int, tuple[TestFailure, ...]]:
    """Return (issue_count, issues) from ruff or mypy output.

    Both emit ``file:line: message``, so one parser covers them. Issues are
    represented as ``TestFailure`` because the debug loop treats "something is
    wrong at this location" identically regardless of which tool said so.
    """
    issues: list[TestFailure] = []
    for match in LINT_ISSUE.finditer(output):
        message = match.group("message").strip()
        # Skip summary lines like "Found 3 errors in 2 files".
        if message.lower().startswith(("found ", "success", "all checks")):
            continue
        issues.append(
            TestFailure(
                test_id=f"{match.group('file')}:{match.group('line')}",
                message=message,
                file_path=match.group("file"),
                line=int(match.group("line")),
            )
        )
        if len(issues) >= limit:
            break
    return len(issues), tuple(issues)


def _last_summary_line(output: str) -> str | None:
    """pytest's summary line, searched from the end.

    Two forms are accepted, because pytest prints different ones depending on
    its flags:

    * decorated — ``===== 1 failed, 4 passed in 0.31s =====``
    * terse — ``1 passed in 0.01s``, printed under ``-q --no-header``

    Searched from the end because a test's own output can contain something that
    looks like a summary line; suites for CLI tools do this constantly.
    """
    for line in reversed(output.splitlines()):
        if line.startswith("=") and PYTEST_COUNT.search(line):
            return line
        if PYTEST_TERSE_SUMMARY.match(line):
            return line
    return None


def _location_from_test_id(test_id: str) -> tuple[str | None, int | None]:
    """``tests/test_db.py::test_rows`` → ``("tests/test_db.py", None)``."""
    if "::" in test_id:
        return test_id.split("::", 1)[0], None
    return (test_id, None) if test_id.endswith(".py") else (None, None)


def tail(text: str, limit: int = 4000) -> str:
    """Keep the end of a long output.

    The end, not the beginning: runners print progress first and the summary of
    what went wrong last.
    """
    return text if len(text) <= limit else "...(truncated)...\n" + text[-limit:]
