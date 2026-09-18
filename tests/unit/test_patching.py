"""Patch application tests.

The design under test: models do exact-text replacement, we compute the diff.
So these check the three outcomes of a text search — found once, not found,
ambiguous — and that the resulting diff is real.

They also check the two rules that are not negotiable: the original clone is
never touched, and no path escapes the workspace.
"""

from __future__ import annotations

import pytest

from patchpilot.models import CodeEdit
from patchpilot.tools.patching import apply_patch, create_working_copy
from patchpilot.tools.workspace import Workspace

SOURCE = '''\
class Table:
    def rows_where(self, where=None):
        if not self.exists():
            return
        return self.db.execute(where)

    def delete_where(self, where=None):
        if not self.exists():
            return self
        return self.db.execute(where)
'''


@pytest.fixture
def repo(tmp_path):
    """A small source repository, plus a workspace to copy it into."""
    source = tmp_path / "repo"
    (source / "src").mkdir(parents=True)
    (source / ".git").mkdir()
    (source / ".git" / "config").write_text("[core]\n")
    (source / "src" / "db.py").write_text(SOURCE)
    (source / "README.md").write_text("# widget\n")
    return source, Workspace(tmp_path / "ws")


def edit(old: str, new: str, path: str = "src/db.py") -> CodeEdit:
    return CodeEdit(file_path=path, old_text=old, new_text=new, reason="test")


# --- the non-negotiables ----------------------------------------------------


def test_the_original_repository_is_never_modified(repo):
    """A failed attempt must cost nothing to recover from, and several attempts
    in the debug loop must each start from the same known state."""
    source, workspace = repo

    apply_patch(
        workspace, source,
        [edit("        if not self.exists():\n            return\n", "")],
        attempt_name="a1",
    )

    assert (source / "src" / "db.py").read_text() == SOURCE


def test_git_is_not_copied_into_the_working_copy(repo):
    """The copy is for editing and testing, not history — and copying .git would
    multiply the size for no benefit."""
    source, workspace = repo

    copy = create_working_copy(workspace, source, "a1")

    assert (copy / "src" / "db.py").exists()
    assert not (copy / ".git").exists()


def test_a_path_escaping_the_workspace_is_rejected(repo):
    """`file_path` comes from a model that was reading untrusted content."""
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [edit("anything", "evil", path="../../../../etc/passwd")],
        attempt_name="a1",
    )

    assert not result.applied
    assert "rejected" in result.failures[0].error


# --- the three outcomes of an exact-text search -----------------------------


def test_a_unique_match_is_applied(repo):
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [edit("    def rows_where(self, where=None):", "    def rows_where(self, where=None, strict=True):")],
        attempt_name="a1",
    )

    assert result.applied
    assert "strict=True" in (result.workspace_path / "src" / "db.py").read_text()


def test_text_that_is_not_present_fails_with_actionable_feedback(repo):
    """'Patch failed' gives the debug loop nothing. This has to say what to do
    differently."""
    source, workspace = repo

    result = apply_patch(
        workspace, source, [edit("def method_that_does_not_exist():", "x")], attempt_name="a1"
    )

    assert not result.applied
    assert "not found" in result.failures[0].error
    assert "exactly" in result.failures[0].error


def test_ambiguous_text_is_refused_rather_than_guessed(repo):
    """`if not self.exists():` appears in both methods. Replacing the first one
    silently would be a plausible-looking wrong fix — the worst outcome."""
    source, workspace = repo

    result = apply_patch(
        workspace, source, [edit("        if not self.exists():", "        if True:")],
        attempt_name="a1",
    )

    assert not result.applied
    assert "appears 2 times" in result.failures[0].error
    assert "more surrounding lines" in result.failures[0].error


def test_disambiguating_with_more_context_succeeds(repo):
    """The remedy the error message asks for actually works."""
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [edit(
            "        if not self.exists():\n            return\n        return self.db.execute(where)",
            "        return self.db.execute(where)",
        )],
        attempt_name="a1",
    )

    assert result.applied


def test_a_missing_file_is_reported_by_name(repo):
    source, workspace = repo

    result = apply_patch(
        workspace, source, [edit("x", "y", path="src/nonexistent.py")], attempt_name="a1"
    )

    assert not result.applied
    assert "does not exist" in result.failures[0].error


# --- the generated diff -----------------------------------------------------


def test_the_diff_is_computed_not_requested(repo):
    """The model never writes hunk headers, so they cannot be arithmetically
    wrong. This checks we produce a real, well-formed unified diff."""
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [edit("        if not self.exists():\n            return\n", "")],
        attempt_name="a1",
    )

    diff = result.patch.diff
    assert "--- a/src/db.py" in diff
    assert "+++ b/src/db.py" in diff
    assert "@@" in diff
    assert "-        if not self.exists():" in diff


def test_line_counts_are_reported(repo):
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [edit("    def rows_where(self, where=None):", "    # a comment\n    def rows_where(self, where=None):")],
        attempt_name="a1",
    )

    assert result.patch.lines_added > result.patch.lines_removed
    assert "src/db.py" in result.patch.files_changed
    assert "+" in result.patch.summary_for_human()


def test_an_unchanged_file_produces_no_diff(repo):
    """Replacing text with itself is a no-op, and must not look like a change."""
    source, workspace = repo

    result = apply_patch(
        workspace, source, [edit("class Table:", "class Table:")], attempt_name="a1"
    )

    assert result.applied
    assert result.patch.diff == ""
    assert result.patch.files_changed == ()


# --- multiple edits ---------------------------------------------------------


def test_several_edits_across_files_are_combined(repo):
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [
            edit("class Table:", "class Table:  # patched"),
            edit("# widget", "# widget (patched)", path="README.md"),
        ],
        attempt_name="a1",
    )

    assert result.applied
    assert result.patch.files_changed == ("README.md", "src/db.py")


def test_one_failed_edit_does_not_stop_the_others(repo):
    """Partial application is reported precisely, so the debug loop knows which
    edit to fix rather than regenerating all of them."""
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [
            edit("class Table:", "class Table:  # ok"),
            edit("this text is absent", "x"),
        ],
        attempt_name="a1",
    )

    assert not result.applied
    assert len(result.failures) == 1
    assert "class Table:  # ok" in (result.workspace_path / "src" / "db.py").read_text()


def test_a_later_edit_can_depend_on_an_earlier_one(repo):
    """Files are re-read per edit rather than snapshotted, so sequential edits
    to the same region work."""
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [
            edit("class Table:", "class Base:\nclass Table:"),
            edit("class Base:", "class Base:  # inserted then edited"),
        ],
        attempt_name="a1",
    )

    assert result.applied
    assert "# inserted then edited" in (result.workspace_path / "src" / "db.py").read_text()


def test_the_failure_report_names_every_failure(repo):
    source, workspace = repo

    result = apply_patch(
        workspace, source,
        [edit("absent one", "x"), edit("absent two", "y", path="README.md")],
        attempt_name="a1",
    )

    report = result.failure_report()
    assert "src/db.py" in report
    assert "README.md" in report


def test_attempts_are_isolated_from_each_other(repo):
    """Each debug-loop attempt starts from the pristine source, not from the
    previous attempt's damage."""
    source, workspace = repo

    first = apply_patch(
        workspace, source, [edit("class Table:", "class Table:  # first")], attempt_name="a1"
    )
    second = apply_patch(
        workspace, source, [edit("class Table:", "class Table:  # second")], attempt_name="a2"
    )

    assert "# first" not in (second.workspace_path / "src" / "db.py").read_text()
    assert first.workspace_path != second.workspace_path
