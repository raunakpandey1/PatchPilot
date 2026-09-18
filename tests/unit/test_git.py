"""Git tests against a repository we build locally — no network.

Building a real repository with real commits, rather than mocking git, because
the thing worth testing is our parsing of git's actual output. A mock would
only prove we can parse the strings we invented.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchpilot.errors import GitError
from patchpilot.tools.git import GitRepository, clone

GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, env={**GIT_ENV, "HOME": str(cwd)}, check=True, capture_output=True)


@pytest.fixture
def sample_repo(tmp_path) -> Path:
    """A repository with three commits and a known shape."""
    repo = tmp_path / "origin"
    repo.mkdir()
    run(["git", "init", "-b", "main"], repo)

    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "Add add()"], repo)

    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text("def test_add():\n    assert True\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "Add a test"], repo)

    (repo / "app.py").write_text("def add(a, b):\n    return a - b  # the bug\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "Introduce the bug"], repo)

    return repo


def test_file_protocol_is_blocked_by_default(sample_repo, tmp_path):
    """The default refuses to clone from the local filesystem.

    Discovered by these very tests failing: the hardening is real enough that
    it blocked a legitimate local clone. Tests opt in explicitly; production
    never does.
    """
    from patchpilot.errors import CloneFailed

    with pytest.raises(CloneFailed, match="transport 'file' not allowed"):
        clone(str(sample_repo), tmp_path / "clone")


def test_clone_and_read_head(sample_repo, tmp_path):
    repo = clone(str(sample_repo), tmp_path / "clone", allow_file_protocol=True)

    assert len(repo.head_sha) == 40
    assert repo.current_branch == "main"
    assert repo.size_mb is not None


def test_list_files_returns_tracked_files_only(sample_repo, tmp_path):
    (sample_repo / "untracked.txt").write_text("not committed")
    repo = clone(str(sample_repo), tmp_path / "clone", allow_file_protocol=True)

    files = repo.list_files()

    assert set(files) == {"app.py", "tests/test_app.py"}
    assert "untracked.txt" not in files


def test_log_returns_commits_newest_first(sample_repo, tmp_path):
    repo = clone(str(sample_repo), tmp_path / "clone", allow_file_protocol=True)

    commits = repo.log(max_count=10)

    assert [c.subject for c in commits] == ["Introduce the bug", "Add a test", "Add add()"]
    assert commits[0].author == "Test"
    assert len(commits[0].short_sha) == 8


def test_log_handles_awkward_commit_subjects(sample_repo, tmp_path):
    """Real commit messages contain commas, pipes and quotes.

    We separate fields with control characters (\\x1f, \\x1e) precisely because
    they cannot appear in a commit subject. Splitting on a comma would break on
    this commit.
    """
    (sample_repo / "x.py").write_text("x = 1\n")
    run(["git", "add", "."], sample_repo)
    run(["git", "commit", "-m", 'fix: a, b | c "quoted" and \\ backslash'], sample_repo)

    repo = clone(str(sample_repo), tmp_path / "clone", allow_file_protocol=True)

    assert repo.log(max_count=1)[0].subject == 'fix: a, b | c "quoted" and \\ backslash'


def test_files_changed_in_commit(sample_repo, tmp_path):
    """This is the free ground truth for measuring retrieval in Phase 3."""
    repo = clone(str(sample_repo), tmp_path / "clone", allow_file_protocol=True)
    bug_commit = repo.log(max_count=1)[0]

    assert repo.files_changed_in(bug_commit.sha) == ("app.py",)


def test_log_can_be_scoped_to_a_path(sample_repo, tmp_path):
    repo = clone(str(sample_repo), tmp_path / "clone", allow_file_protocol=True)

    commits = repo.log(max_count=10, paths=["tests/test_app.py"])

    assert [c.subject for c in commits] == ["Add a test"]


def test_show_file_reads_a_historical_version(sample_repo, tmp_path):
    repo = clone(str(sample_repo), tmp_path / "clone", allow_file_protocol=True)
    first_commit = repo.log(max_count=10)[-1]

    assert "a + b" in repo.show_file(first_commit.sha, "app.py")
    assert "a - b" in (repo.path / "app.py").read_text()


def test_blame_identifies_the_commit_for_a_line(sample_repo, tmp_path):
    repo = clone(str(sample_repo), tmp_path / "clone", allow_file_protocol=True)

    sha = repo.blame_line("app.py", 2)

    assert sha == repo.log(max_count=1)[0].sha


def test_clone_refuses_an_existing_destination(sample_repo, tmp_path):
    destination = tmp_path / "clone"
    clone(str(sample_repo), destination, allow_file_protocol=True)

    with pytest.raises(GitError, match="already exists"):
        clone(str(sample_repo), destination, allow_file_protocol=True)


def test_clone_enforces_the_size_budget(sample_repo, tmp_path):
    """A repository is untrusted input; the disk budget is enforced, not hoped for."""
    from patchpilot.errors import RepositoryTooLarge

    with pytest.raises(RepositoryTooLarge):
        clone(str(sample_repo), tmp_path / "clone", max_size_mb=0, allow_file_protocol=True)

    assert not (tmp_path / "clone").exists(), "an over-budget clone must be cleaned up"


def test_non_repository_directory_is_rejected(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(GitError, match="not a git repository"):
        GitRepository(plain)
