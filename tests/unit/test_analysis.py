"""Repository analyzer tests.

The analyzer is pure lookup — it reads files that already exist and reports
what they say. So the tests build small repositories with known contents and
check the report. No LLM, no network, no mocking.

These matter more than they look. Every field here is consumed by a later
phase, and a wrong answer surfaces three phases downstream disguised as
something else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from patchpilot.analysis.repository import analyze_repository
from patchpilot.models import PackageManager, Repository, TestRunner
from patchpilot.tools.git import GitRepository

REPOSITORY = Repository(
    full_name="acme/widget",
    owner="acme",
    name="widget",
    default_branch="main",
    clone_url="https://github.com/acme/widget.git",
    html_url="https://github.com/acme/widget",
)

GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def make_repo(tmp_path: Path, files: dict[str, str]) -> GitRepository:
    """Create a committed git repository containing exactly ``files``."""
    root = tmp_path / "repo"
    root.mkdir()
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    env = {**GIT_ENV, "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-b", "main"], cwd=root, env=env, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, env=env, check=True, capture_output=True)
    return GitRepository(root)


POETRY_PYPROJECT = """
[tool.poetry]
name = "widget"

[tool.poetry.dependencies]
python = "^3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 100

[tool.mypy]
strict = true
"""


def test_detects_a_poetry_pytest_project(tmp_path):
    clone = make_repo(tmp_path, {
        "pyproject.toml": POETRY_PYPROJECT,
        "src/widget/core.py": "def go():\n    return 1\n",
        "tests/test_core.py": "def test_go():\n    assert True\n",
        "tests/conftest.py": "",
        "README.md": "# widget",
        ".github/workflows/ci.yml": "on: push",
    })

    snapshot = analyze_repository(REPOSITORY, clone)

    assert snapshot.package_manager is PackageManager.POETRY
    assert snapshot.test_runner is TestRunner.PYTEST
    assert snapshot.test_command == ("python", "-m", "pytest", "-x", "-q", "--no-header")
    assert snapshot.lint_command == ("python", "-m", "ruff", "check", ".")
    assert snapshot.typecheck_command == ("python", "-m", "mypy", ".")
    assert snapshot.python_requires == "^3.11"
    assert snapshot.has_ci is True
    assert snapshot.is_testable is True


def test_source_and_test_files_are_separated(tmp_path):
    """Later phases depend on this split.

    RAG should weight production code above test fixtures, and the patch
    generator must know which files are tests so it cannot 'fix' a failure by
    deleting the test that caught it.
    """
    clone = make_repo(tmp_path, {
        "pyproject.toml": POETRY_PYPROJECT,
        "src/widget/core.py": "x = 1",
        "src/widget/util.py": "y = 2",
        "tests/test_core.py": "def test(): pass",
        "docs/guide.md": "# guide",
    })

    snapshot = analyze_repository(REPOSITORY, clone)

    assert set(snapshot.source_files) == {"src/widget/core.py", "src/widget/util.py"}
    assert snapshot.test_files == ("tests/test_core.py",)
    assert snapshot.doc_files == ("docs/guide.md",)
    assert "pyproject.toml" in snapshot.config_files


@pytest.mark.parametrize(
    ("path", "is_test"),
    [
        ("tests/test_x.py", True),
        ("test/test_x.py", True),
        ("src/pkg/test_helpers.py", True),
        ("src/pkg/helpers_test.py", True),
        ("conftest.py", True),
        ("src/pkg/contest.py", False),
        ("src/latest/thing.py", False),
    ],
)
def test_test_file_detection_rules(tmp_path, path, is_test):
    """`latest/` must not be mistaken for a test directory — substring matching
    on 'test' is the obvious implementation and the wrong one."""
    clone = make_repo(tmp_path, {"pyproject.toml": POETRY_PYPROJECT, path: "x = 1"})

    snapshot = analyze_repository(REPOSITORY, clone)

    assert (path in snapshot.test_files) is is_test


def test_setuptools_and_unittest_project(tmp_path):
    clone = make_repo(tmp_path, {
        "setup.py": "from setuptools import setup\nsetup()",
        "widget/core.py": "x = 1",
        "tests/test_core.py": "import unittest",
    })

    snapshot = analyze_repository(REPOSITORY, clone)

    assert snapshot.package_manager is PackageManager.SETUPTOOLS
    assert snapshot.test_runner is TestRunner.UNITTEST
    assert snapshot.test_command == ("python", "-m", "unittest", "discover")


def test_repository_with_no_tests_is_not_testable(tmp_path):
    """The agent must refuse work it cannot validate.

    Without a test command there is no way to tell a fix from a break, so this
    flag is what stops PatchPilot from confidently proposing unverifiable
    patches.
    """
    clone = make_repo(tmp_path, {"README.md": "# docs only", "index.html": "<p>hi</p>"})

    snapshot = analyze_repository(REPOSITORY, clone)

    assert snapshot.test_command is None
    assert snapshot.is_testable is False


def test_malformed_pyproject_does_not_crash(tmp_path):
    """A broken config is the repository's problem, not a crash for us."""
    clone = make_repo(tmp_path, {
        "pyproject.toml": "this is [[not valid toml",
        "app.py": "x = 1",
    })

    snapshot = analyze_repository(REPOSITORY, clone)

    assert snapshot.package_manager is PackageManager.UNKNOWN


def test_languages_ignore_the_long_tail(tmp_path):
    """One stray shell script must not make this 'partly a Shell project'."""
    files = {f"src/mod{i}.py": "x = 1" for i in range(30)}
    files["scripts/build.sh"] = "echo hi"
    files["pyproject.toml"] = POETRY_PYPROJECT
    clone = make_repo(tmp_path, files)

    snapshot = analyze_repository(REPOSITORY, clone)

    assert snapshot.languages == ("Python",)


def test_test_command_is_an_argument_list_not_a_string(tmp_path):
    """A string would need a shell to run it, and a shell interprets `;` and
    backticks — in a value derived from an untrusted repository."""
    clone = make_repo(tmp_path, {"pyproject.toml": POETRY_PYPROJECT, "tests/test_a.py": "x = 1"})

    snapshot = analyze_repository(REPOSITORY, clone)

    assert isinstance(snapshot.test_command, tuple)
    assert all(isinstance(part, str) for part in snapshot.test_command)


def test_snapshot_summary_is_human_readable(tmp_path):
    clone = make_repo(tmp_path, {"pyproject.toml": POETRY_PYPROJECT, "tests/test_a.py": "x = 1"})

    summary = analyze_repository(REPOSITORY, clone).summary()

    assert "acme/widget" in summary
    assert "pytest" in summary
