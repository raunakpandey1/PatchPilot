"""Fix planning and patch generation tests.

Both nodes call a model, so the interesting cases are the checks applied
*afterwards*: a plan that names files the diagnosis never cited, a patch whose
edits do not apply, a model that declines. The prompt asking for something is
not evidence that it happened.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import httpx
import pytest

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.nodes.fixing import (
    EditList,
    make_generate_patch_node,
    make_plan_fix_node,
)
from patchpilot.agent.prompts.fixing import (
    PATCH_SYSTEM,
    build_patch_prompt,
    extract_regions,
)
from patchpilot.agent.state import AgentState
from patchpilot.analysis.repository import analyze_repository
from patchpilot.llm.base import LLMUnavailable
from patchpilot.llm.fake import FakeProvider
from patchpilot.models import (
    CodeEdit,
    Confidence,
    Evidence,
    FixPlan,
    Issue,
    IssueState,
    Repository,
    RootCause,
)
from patchpilot.tools.git import GitRepository
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.workspace import Workspace

NOW = datetime(2026, 1, 1, tzinfo=UTC)
REPO = "acme/widget"

REPOSITORY = Repository(
    full_name=REPO, owner="acme", name="widget", default_branch="main",
    clone_url="https://github.com/acme/widget.git", html_url="https://github.com/acme/widget",
)

ISSUE = Issue(
    number=1, title="rows_where returns nothing for a missing table",
    body="Expected an error.", state=IssueState.OPEN,
    created_at=NOW, updated_at=NOW, html_url="https://github.com/acme/widget/issues/1",
)

DB_SOURCE = '''\
class Table:
    def rows_where(self, where=None):
        if not self.exists():
            return
        return self.db.execute(where)
'''

ROOT_CAUSE = RootCause(
    summary="rows_where returns early instead of raising",
    explanation="the existence check suppresses the error",
    primary_file="src/db.py",
    evidence=(Evidence(file_path="src/db.py", start_line=2, end_line=5,
                       why_relevant="the early return"),),
    confidence=Confidence.HIGH,
)

PLAN = FixPlan(
    approach="remove the early return",
    files_to_change=("src/db.py",),
    steps=("delete the exists() guard",),
    test_strategy="existing tests",
)

GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


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


def make_deps(tmp_path, llm: FakeProvider) -> AgentDeps:
    return AgentDeps(
        github=GitHubClient(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
        workspace=Workspace(tmp_path / "ws"),
        llm=llm,
    )


def base_state(snapshot) -> AgentState:
    return AgentState(
        run_id="r1", repository_full_name=REPO, snapshot=snapshot,
        selected_issue=ISSUE, root_cause=ROOT_CAUSE, fix_plan=PLAN, debug_attempts=0,
    )


# --- planning ---------------------------------------------------------------


def test_a_good_plan_passes_through(tmp_path, snapshot):
    node = make_plan_fix_node(make_deps(tmp_path, FakeProvider(responses=[PLAN])))

    result = node(base_state(snapshot))

    assert not result.get("halted")
    assert result["fix_plan"].files_to_change == ("src/db.py",)


def test_a_plan_beyond_the_evidence_is_rejected(tmp_path, snapshot):
    """A plan naming files the diagnosis never cited means the model went beyond
    what it was shown. Cheap to catch precisely because planning is separate."""
    overreaching = PLAN.model_copy(
        update={"files_to_change": ("src/db.py", "src/unrelated.py")}
    )
    node = make_plan_fix_node(make_deps(tmp_path, FakeProvider(responses=[overreaching])))

    result = node(base_state(snapshot))

    assert result["halted"]
    assert "beyond the evidence" in result["halt_reason"]
    assert "src/unrelated.py" in result["errors"][0]


def test_a_plan_with_no_files_halts(tmp_path, snapshot):
    empty = PLAN.model_copy(update={"files_to_change": ()})
    node = make_plan_fix_node(make_deps(tmp_path, FakeProvider(responses=[empty])))

    assert node(base_state(snapshot))["halted"]


def test_planning_without_a_root_cause_spends_nothing(tmp_path, snapshot):
    llm = FakeProvider(responses=[PLAN])
    node = make_plan_fix_node(make_deps(tmp_path, llm))

    result = node(AgentState(run_id="r", repository_full_name=REPO, snapshot=snapshot))

    assert result["halted"]
    assert llm.call_count == 0


def test_the_plan_prompt_forbids_deleting_tests():
    """The cheapest way to make a failing test pass is to delete it. That has to
    be ruled out explicitly, in the system prompt and later in policy."""
    from patchpilot.agent.prompts.fixing import PLAN_SYSTEM

    assert "NEVER plan to delete, skip or weaken a test" in PLAN_SYSTEM


def test_provider_failure_during_planning_halts(tmp_path, snapshot):
    node = make_plan_fix_node(
        make_deps(tmp_path, FakeProvider(responses=[LLMUnavailable("503")]))
    )

    result = node(base_state(snapshot))

    assert result["halted"]
    assert "503" in result["errors"][0]


# --- region extraction ------------------------------------------------------


def test_only_files_in_both_the_plan_and_the_diagnosis_are_read(tmp_path, snapshot):
    """A plan that invents a file gets no content for it, so any edit to it
    fails a check rather than silently touching something unexamined."""
    plan = PLAN.model_copy(update={"files_to_change": ("src/db.py", "src/ghost.py")})

    regions = extract_regions(snapshot.local_path, ROOT_CAUSE, plan)

    assert {path for path, _, _ in regions} == {"src/db.py"}


def test_extracted_regions_contain_the_cited_code(tmp_path, snapshot):
    regions = extract_regions(snapshot.local_path, ROOT_CAUSE, PLAN)

    assert regions
    assert "if not self.exists():" in regions[0][2]


def test_the_patch_prompt_shows_line_numbers_but_forbids_using_them(tmp_path, snapshot):
    """Numbers help the model orient; doing arithmetic on them is exactly what
    it gets wrong, which is why we compute the diff ourselves."""
    regions = extract_regions(snapshot.local_path, ROOT_CAUSE, PLAN)

    prompt = build_patch_prompt(ISSUE, ROOT_CAUSE, PLAN, regions)

    assert "|" in prompt  # the numbered gutter
    assert "NOT part of the file" in prompt
    assert "Do NOT use line numbers" in PATCH_SYSTEM


# --- patch generation -------------------------------------------------------


def test_a_valid_edit_produces_a_real_diff(tmp_path, snapshot):
    edits = EditList(
        edits=(
            CodeEdit(
                file_path="src/db.py",
                old_text="        if not self.exists():\n            return\n",
                new_text="",
                reason="remove the guard",
            ),
        ),
        explanation="removes the early return",
    )
    node = make_generate_patch_node(make_deps(tmp_path, FakeProvider(responses=[edits])))

    result = node(base_state(snapshot))

    patch = result["patch"]
    assert patch.files_changed == ("src/db.py",)
    assert patch.lines_removed == 2
    assert "@@" in patch.diff
    assert not result.get("patch_errors")


def test_an_edit_that_does_not_apply_is_repairable_not_fatal(tmp_path, snapshot):
    """This is what the Phase 7 debug loop exists to fix, so it must not halt
    the run — and the message must be specific enough to act on."""
    edits = EditList(
        edits=(CodeEdit(file_path="src/db.py", old_text="text that is absent",
                        new_text="x", reason="r"),),
    )
    node = make_generate_patch_node(make_deps(tmp_path, FakeProvider(responses=[edits])))

    result = node(base_state(snapshot))

    assert not result.get("halted"), "a failed apply is repairable, not run-ending"
    assert result["patch_errors"]
    assert "not found" in result["patch_errors"][0]


def test_a_model_declining_is_a_valid_outcome(tmp_path, snapshot):
    """Better than a guessed edit. The explanation is kept for the human."""
    declined = EditList(edits=(), explanation="the excerpt does not show the caller")
    node = make_generate_patch_node(make_deps(tmp_path, FakeProvider(responses=[declined])))

    result = node(base_state(snapshot))

    assert result["halted"]
    assert "no edits" in result["halt_reason"]
    assert "caller" in result["halt_reason"]


def test_the_original_repository_is_untouched_by_generation(tmp_path, snapshot):
    edits = EditList(
        edits=(CodeEdit(file_path="src/db.py",
                        old_text="        if not self.exists():\n            return\n",
                        new_text="", reason="r"),),
    )
    node = make_generate_patch_node(make_deps(tmp_path, FakeProvider(responses=[edits])))

    node(base_state(snapshot))

    assert (snapshot.local_path / "src" / "db.py").read_text() == DB_SOURCE


def test_generation_without_a_plan_spends_nothing(tmp_path, snapshot):
    llm = FakeProvider(responses=[EditList()])
    node = make_generate_patch_node(make_deps(tmp_path, llm))

    state = base_state(snapshot)
    state["fix_plan"] = None
    result = node(state)

    assert result["halted"]
    assert llm.call_count == 0


def test_the_model_is_shown_the_actual_file_contents(tmp_path, snapshot):
    """It must return `old_text` character for character, which it can only do
    if it was shown the file exactly."""
    llm = FakeProvider(responses=[EditList()])
    node = make_generate_patch_node(make_deps(tmp_path, llm))

    node(base_state(snapshot))

    assert "if not self.exists():" in llm.last_call.prompt_text
    assert llm.last_call.schema == "EditList"
