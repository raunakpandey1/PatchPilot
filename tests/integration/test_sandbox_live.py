"""Sandbox tests against a real Docker daemon.

The unit tests assert that the runner *asks* for the right controls. These
assert that Docker actually enforces them — which is the only thing that
matters, and the one thing a stub can never tell you.

    poetry run pytest -m integration tests/integration/test_sandbox_live.py

Requires Docker to be running. The first run pulls a ~130 MB base image.
"""

from __future__ import annotations

import subprocess

import pytest

from patchpilot.analysis.repository import analyze_repository
from patchpilot.models import CheckOutcome, Repository
from patchpilot.sandbox.docker_runner import DockerSandbox
from patchpilot.tools.git import GitRepository

pytestmark = pytest.mark.integration

REPOSITORY = Repository(
    full_name="acme/widget", owner="acme", name="widget", default_branch="main",
    clone_url="https://github.com/acme/widget.git", html_url="https://github.com/acme/widget",
)

GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}

PYPROJECT = """\
[project]
name = "widget"
version = "0.1.0"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


def build_repo(tmp_path, test_body: str):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "widget.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "tests" / "test_widget.py").write_text(test_body)

    env = {**GIT_ENV, "HOME": str(tmp_path)}
    for args in (["git", "init", "-b", "main"], ["git", "add", "."], ["git", "commit", "-m", "x"]):
        subprocess.run(args, cwd=root, env=env, check=True, capture_output=True)

    return analyze_repository(REPOSITORY, GitRepository(root))


@pytest.fixture(scope="module")
def sandbox():
    box = DockerSandbox(timeout_s=180)
    if not box.available():
        pytest.skip("Docker daemon is not running")
    return box


def test_a_passing_suite_passes(sandbox, tmp_path):
    snapshot = build_repo(
        tmp_path, "from widget import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    image = sandbox.prepare_image(snapshot, tag="patchpilot-test-pass:latest")

    result = sandbox.run_checks(image, snapshot.local_path, snapshot, include_lint=False)

    assert result.passed, result.summary_for_human()
    assert result.tests.passed_count == 1


def test_a_failing_suite_reports_the_failing_test(sandbox, tmp_path):
    snapshot = build_repo(
        tmp_path,
        "from widget import add\n\n"
        "def test_add():\n    assert add(1, 2) == 3\n\n"
        "def test_broken():\n    assert add(1, 1) == 3, 'deliberate failure'\n",
    )
    image = sandbox.prepare_image(snapshot, tag="patchpilot-test-fail:latest")

    result = sandbox.run_checks(image, snapshot.local_path, snapshot, include_lint=False)

    assert not result.passed
    assert result.tests.failed_count == 1
    assert any("test_broken" in f.test_id for f in result.failures)
    assert "test_broken" in result.feedback_for_repair()


def test_network_egress_is_actually_blocked(sandbox, tmp_path):
    """The control that matters most, verified rather than assumed.

    A test that can reach the internet can exfiltrate whatever the process can
    see. This runs a test that *tries* to connect and asserts it cannot.
    """
    snapshot = build_repo(
        tmp_path,
        "import socket\n\n"
        "def test_cannot_reach_the_internet():\n"
        "    socket.setdefaulttimeout(5)\n"
        "    try:\n"
        "        socket.create_connection(('1.1.1.1', 53), timeout=5)\n"
        "    except OSError:\n"
        "        return  # expected: no network\n"
        "    raise AssertionError('NETWORK WAS REACHABLE — the sandbox is not isolating')\n",
    )
    image = sandbox.prepare_image(snapshot, tag="patchpilot-test-net:latest")

    result = sandbox.run_checks(image, snapshot.local_path, snapshot, include_lint=False)

    assert result.passed, (
        "the container reached the network — egress is not blocked:\n"
        + result.summary_for_human()
    )


def test_the_host_filesystem_is_not_visible(sandbox, tmp_path):
    """Only the working copy is mounted. The rest of the machine is not there."""
    snapshot = build_repo(
        tmp_path,
        "import os\n\n"
        "def test_host_home_is_not_mounted():\n"
        "    assert not os.path.exists('/Users'), 'the host filesystem is visible'\n"
        "    assert os.path.exists('/repo/widget.py'), 'the working copy should be mounted'\n",
    )
    image = sandbox.prepare_image(snapshot, tag="patchpilot-test-fs:latest")

    result = sandbox.run_checks(image, snapshot.local_path, snapshot, include_lint=False)

    assert result.passed, result.summary_for_human()


def test_the_container_does_not_run_as_root(sandbox, tmp_path):
    snapshot = build_repo(
        tmp_path,
        "import os\n\ndef test_not_root():\n    assert os.getuid() != 0, 'running as root'\n",
    )
    image = sandbox.prepare_image(snapshot, tag="patchpilot-test-user:latest")

    result = sandbox.run_checks(image, snapshot.local_path, snapshot, include_lint=False)

    assert result.passed, result.summary_for_human()


def test_a_hanging_test_is_killed(sandbox, tmp_path):
    """Docker provides no run timeout, so it is enforced from outside. Without
    this a single test could hold a run open forever."""
    snapshot = build_repo(
        tmp_path, "import time\n\ndef test_hangs():\n    time.sleep(600)\n"
    )
    image = sandbox.prepare_image(snapshot, tag="patchpilot-test-hang:latest")

    box = DockerSandbox(timeout_s=10)
    result = box.run_checks(image, snapshot.local_path, snapshot, include_lint=False)

    assert result.tests.outcome is CheckOutcome.TIMEOUT
    assert not result.passed
