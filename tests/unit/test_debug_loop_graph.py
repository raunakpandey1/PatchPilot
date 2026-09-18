"""The debug loop, exercised through the real graph.

The routing function is tested in isolation elsewhere. These run the actual
cycle — validate → record → repair → validate — with a scripted model and a stub
sandbox, and assert it both *iterates* and *terminates*.

A loop that cannot be shown to terminate is the single most expensive bug
available in this project: on a free tier it is a day's quota.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.graph import build_graph
from patchpilot.agent.nodes.fixing import EditList
from patchpilot.agent.state import AgentState, initial_state
from patchpilot.analysis.repository import analyze_repository
from patchpilot.llm.fake import FakeProvider
from patchpilot.models import (
    CheckOutcome,
    CheckResult,
    CodeEdit,
    Confidence,
    Evidence,
    FixPlan,
    Issue,
    IssueState,
    Repository,
    RootCause,
    TestFailure,
    ValidationResult,
)
from patchpilot.tools.git import GitRepository
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.workspace import Workspace

NOW = datetime(2026, 1, 1, tzinfo=UTC)
REPOSITORY = Repository(
    full_name="acme/widget", owner="acme", name="widget", default_branch="main",
    clone_url="https://github.com/acme/widget.git", html_url="https://github.com/acme/widget",
)

DB_SOURCE = "class Table:\n    def rows_where(self, w):\n        if not self.exists():\n            return\n        return self.db.execute(w)\n"

ROOT_CAUSE = RootCause(
    summary="early return", explanation="x", primary_file="src/db.py",
    evidence=(Evidence(file_path="src/db.py", start_line=1, end_line=5, why_relevant="y"),),
    confidence=Confidence.HIGH,
)
PLAN = FixPlan(
    approach="remove guard", files_to_change=("src/db.py",),
    steps=("delete",), test_strategy="pytest",
)
ISSUE = Issue(
    number=1, title="rows_where returns nothing for a missing table",
    body="Expected an error, got an empty result.", state=IssueState.OPEN,
    created_at=NOW, updated_at=NOW, html_url="https://github.com/acme/widget/issues/1",
)

GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def failing(*ids: str) -> ValidationResult:
    return ValidationResult(checks=(
        CheckResult(name="tests", command=("pytest",), outcome=CheckOutcome.FAILED,
                    exit_code=1, failed_count=len(ids),
                    failures=tuple(TestFailure(test_id=i, message="boom") for i in ids)),
    ))


def passing() -> ValidationResult:
    return ValidationResult(checks=(
        CheckResult(name="tests", command=("pytest",), outcome=CheckOutcome.PASSED,
                    exit_code=0, passed_count=9),
    ))


class ScriptedSandbox:
    """Returns a scripted sequence of validation results.

    Past the end, the last result repeats — so a test for "the loop gives up"
    needs a sandbox that keeps failing, not one that runs out.
    """

    def __init__(self, results: list[ValidationResult]) -> None:
        self._results = results
        self.runs = 0

    def prepare_image(self, snapshot, *, tag):
        return "stub:latest"

    def run_checks(self, image, working_copy, snapshot, **kwargs):
        result = self._results[min(self.runs, len(self._results) - 1)]
        self.runs += 1
        return result


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['tests']\n")
    (root / "src" / "db.py").write_text(DB_SOURCE)
    (root / "tests" / "test_db.py").write_text("def test_x(): assert True\n")
    env = {**GIT_ENV, "HOME": str(tmp_path)}
    for args in (["git", "init", "-b", "main"], ["git", "add", "."], ["git", "commit", "-m", "x"]):
        subprocess.run(args, cwd=root, env=env, check=True, capture_output=True)
    return analyze_repository(REPOSITORY, GitRepository(root))


def valid_edit(marker: str) -> EditList:
    """An edit that applies cleanly, varied so each attempt differs."""
    return EditList(
        edits=(CodeEdit(file_path="src/db.py", old_text="class Table:",
                        new_text=f"class Table:  # {marker}", reason="r"),),
        explanation=marker,
    )


def run_loop(tmp_path, snapshot, *, llm_script, sandbox, max_attempts=3):
    """Drive the loop directly from generate_patch, the way the graph does."""
    from patchpilot.agent.nodes.debugging import (
        make_record_failure_node,
        make_repair_patch_node,
        route_after_validation,
    )
    from patchpilot.agent.nodes.fixing import make_generate_patch_node
    from patchpilot.agent.nodes.validation import make_validate_patch_node

    deps = AgentDeps(
        github=GitHubClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        workspace=Workspace(tmp_path / "ws"),
        llm=FakeProvider(responses=llm_script),
        sandbox=sandbox,
        validate_lint=False,
    )
    state = AgentState(
        run_id="loop", repository_full_name="acme/widget", snapshot=snapshot,
        selected_issue=ISSUE, root_cause=ROOT_CAUSE, fix_plan=PLAN,
        debug_attempts=0, max_debug_attempts=max_attempts,
        failure_signatures=[], visited=[], errors=[], patch_errors=[],
    )
    state = {**state, **make_generate_patch_node(deps)(state)}

    for _ in range(20):  # a hard stop so a broken loop fails the test, not the machine
        state = {**state, **make_validate_patch_node(deps)(state)}
        out = make_record_failure_node(deps)(state)
        state = {
            **state,
            **out,
            "failure_signatures": state["failure_signatures"] + out.get("failure_signatures", []),
        }
        route = route_after_validation(state)
        if route != "repair":
            return state, route
        repaired = make_repair_patch_node(deps)(state)
        state = {**state, **repaired}
        if state.get("halted"):
            return state, "give_up"
    raise AssertionError("the loop did not terminate")


# --- the loop actually loops ------------------------------------------------


def test_a_patch_that_passes_first_time_ends_immediately(tmp_path, snapshot):
    sandbox = ScriptedSandbox([passing()])

    state, route = run_loop(
        tmp_path, snapshot, llm_script=[valid_edit("first")], sandbox=sandbox
    )

    assert route == "done"
    assert state["debug_attempts"] == 0
    assert sandbox.runs == 1


def test_a_failure_then_a_fix_takes_one_repair(tmp_path, snapshot):
    """The loop's reason for existing: the first attempt is wrong, the test
    output says why, the second attempt works."""
    sandbox = ScriptedSandbox([failing("t::a"), passing()])

    state, route = run_loop(
        tmp_path, snapshot,
        llm_script=[valid_edit("first"), valid_edit("repaired")],
        sandbox=sandbox,
    )

    assert route == "done"
    assert state["debug_attempts"] == 1
    assert sandbox.runs == 2
    assert "repaired" in (Path(state["working_copy"]) / "src" / "db.py").read_text()


def test_the_loop_gives_up_at_the_attempt_limit(tmp_path, snapshot):
    """Different failures every time, so loop detection never fires — this is
    the blunt bound doing its job."""
    sandbox = ScriptedSandbox([
        failing("t::a"), failing("t::b"), failing("t::c"), failing("t::d"), failing("t::e"),
    ])

    state, route = run_loop(
        tmp_path, snapshot,
        llm_script=[valid_edit(f"try{i}") for i in range(6)],
        sandbox=sandbox, max_attempts=3,
    )

    assert route == "give_up"
    assert state["debug_attempts"] == 3
    assert sandbox.runs == 4  # the initial run plus three repairs


def test_repeated_identical_failures_stop_early(tmp_path, snapshot):
    """The bound that saves quota. With a limit of 5, an unchanging failure
    stops after 2 — three model calls not spent learning the same thing."""
    sandbox = ScriptedSandbox([failing("t::a")])

    state, route = run_loop(
        tmp_path, snapshot,
        llm_script=[valid_edit(f"try{i}") for i in range(10)],
        sandbox=sandbox, max_attempts=5,
    )

    assert route == "give_up"
    assert state["debug_attempts"] < 5, "loop detection should stop before the limit"
    assert sandbox.runs <= 3


def test_the_model_declining_to_repair_ends_the_loop(tmp_path, snapshot):
    """After seeing real failure output, 'the diagnosis was wrong' is a
    meaningful answer and better than forcing a change."""
    sandbox = ScriptedSandbox([failing("t::a")])

    state, route = run_loop(
        tmp_path, snapshot,
        llm_script=[
            valid_edit("first"),
            EditList(edits=(), explanation="the diagnosis does not explain this failure"),
        ],
        sandbox=sandbox,
    )

    assert route == "give_up"
    assert state["halted"]
    assert "diagnosis does not explain" in state["halt_reason"]


def test_each_attempt_starts_from_the_pristine_source(tmp_path, snapshot):
    """Attempts must not accumulate each other's damage."""
    sandbox = ScriptedSandbox([failing("t::a"), failing("t::b"), passing()])

    state, _ = run_loop(
        tmp_path, snapshot,
        llm_script=[valid_edit("one"), valid_edit("two"), valid_edit("three")],
        sandbox=sandbox,
    )

    final = (Path(state["working_copy"]) / "src" / "db.py").read_text()
    assert "# three" in final
    assert "# one" not in final and "# two" not in final


def test_the_original_repository_is_never_touched_by_the_loop(tmp_path, snapshot):
    sandbox = ScriptedSandbox([failing("t::a"), passing()])

    run_loop(
        tmp_path, snapshot,
        llm_script=[valid_edit("one"), valid_edit("two")], sandbox=sandbox,
    )

    assert (snapshot.local_path / "src" / "db.py").read_text() == DB_SOURCE


def test_a_sandbox_failure_does_not_loop_forever(tmp_path, snapshot):
    """'We could not tell' repeated is still no progress."""
    sandbox = ScriptedSandbox([ValidationResult(sandbox_error="daemon down")])

    state, route = run_loop(
        tmp_path, snapshot,
        llm_script=[valid_edit(f"try{i}") for i in range(10)],
        sandbox=sandbox, max_attempts=5,
    )

    assert route == "give_up"
    assert state["debug_attempts"] < 5


def test_the_repair_prompt_receives_the_real_failure(tmp_path, snapshot):
    """Not a summary of the failure — the actual test output. Otherwise the loop
    reasons about its own description of reality."""
    llm = FakeProvider(responses=[valid_edit("first"), valid_edit("second")])
    deps = AgentDeps(
        github=GitHubClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        workspace=Workspace(tmp_path / "ws"), llm=llm,
        sandbox=ScriptedSandbox([failing("tests/test_db.py::test_specific_name")]),
        validate_lint=False,
    )
    from patchpilot.agent.nodes.debugging import make_repair_patch_node
    from patchpilot.agent.nodes.fixing import make_generate_patch_node
    from patchpilot.agent.nodes.validation import make_validate_patch_node

    state = AgentState(
        run_id="loop", repository_full_name="acme/widget", snapshot=snapshot,
        selected_issue=ISSUE, root_cause=ROOT_CAUSE, fix_plan=PLAN, debug_attempts=0,
        max_debug_attempts=3, failure_signatures=[],
    )
    state = {**state, **make_generate_patch_node(deps)(state)}
    state = {**state, **make_validate_patch_node(deps)(state)}
    make_repair_patch_node(deps)(state)

    assert "test_specific_name" in llm.last_call.prompt_text


def test_the_graph_contains_the_cycle():
    """The edge that makes this an agent rather than a pipeline."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        graph = build_graph(AgentDeps(
            github=GitHubClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
            workspace=Workspace(Path(d)), llm=FakeProvider(),
        ))
        edges = {(e.source, e.target) for e in graph.get_graph().edges}

    assert ("validate_patch", "record_failure") in edges
    assert ("record_failure", "repair_patch") in edges
    assert ("repair_patch", "validate_patch") in edges, "the cycle must close"


def test_initial_state_carries_the_bound():
    assert initial_state("r", "a/b", max_debug_attempts=7)["max_debug_attempts"] == 7
