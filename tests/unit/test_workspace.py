"""Workspace confinement tests.

These are security tests. Each one is a real technique for escaping a directory,
and each must be blocked. If any of these ever fails, untrusted repository
content can reach the host filesystem.
"""

from __future__ import annotations

import os

import pytest

from patchpilot.errors import WorkspaceError
from patchpilot.tools.workspace import Workspace


@pytest.fixture
def workspace(tmp_path):
    return Workspace(tmp_path / "ws")


def test_paths_inside_are_allowed(workspace):
    resolved = workspace.resolve("repos/owner__name/src/main.py")
    assert resolved.is_relative_to(workspace.root)


@pytest.mark.parametrize(
    "attack",
    [
        "../outside",
        "../../outside",
        "repos/../../outside",
        "/etc/passwd",
        "repos/owner/../../../../tmp/evil",
    ],
)
def test_traversal_is_blocked(workspace, attack):
    with pytest.raises(WorkspaceError):
        workspace.resolve(attack)


def test_symlink_escape_is_blocked(workspace):
    """The attack that catches naive string-prefix checks.

    ``ws/link/passwd`` starts with the workspace path as a *string*, but
    resolves to ``/etc/passwd``. Only following the symlink reveals it — which
    is why we use ``Path.resolve()`` and not ``str.startswith()``.
    """
    link = workspace.root / "link"
    os.symlink("/etc", link)

    with pytest.raises(WorkspaceError):
        workspace.resolve("link/passwd")


def test_is_inside_is_the_non_raising_form(workspace):
    assert workspace.is_inside("repos/x") is True
    assert workspace.is_inside("../x") is False


def test_repo_dir_flattens_the_name(workspace):
    """An untrusted name must not create directory nesting we did not intend."""
    assert workspace.repo_dir("simonw/sqlite-utils").name == "simonw__sqlite-utils"
    assert workspace.repo_dir("../../etc").is_relative_to(workspace.root)


def test_clear_refuses_to_delete_the_root(workspace):
    with pytest.raises(WorkspaceError):
        workspace.clear(workspace.root)


def test_clear_removes_a_subdirectory(workspace):
    target = workspace.resolve("repos/doomed")
    target.mkdir(parents=True)
    (target / "file.txt").write_text("x")

    workspace.clear("repos/doomed")

    assert not target.exists()


def test_size_bytes_counts_files(workspace):
    path = workspace.resolve("repos/sized")
    path.mkdir(parents=True)
    (path / "a.txt").write_text("12345")

    assert workspace.size_bytes("repos/sized") == 5
