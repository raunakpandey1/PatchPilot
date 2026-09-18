"""Sandbox tests using a stubbed Docker client.

The container controls themselves are asserted here — that the runner *asks* for
no network, a read-only root, memory and PID limits, dropped capabilities and a
non-root user. Those arguments are the security boundary, and a refactor that
silently drops one would otherwise go unnoticed.

Whether Docker honours them is Docker's job, and is checked by the integration
test.
"""

from __future__ import annotations

import pytest

from patchpilot.models import (
    CheckOutcome,
    Repository,
    RepositorySnapshot,
    TestRunner,
)
from patchpilot.sandbox.docker_runner import DockerSandbox, SandboxError

REPOSITORY = Repository(
    full_name="acme/widget", owner="acme", name="widget", default_branch="main",
    clone_url="https://github.com/acme/widget.git", html_url="https://github.com/acme/widget",
)

PASSING = "===== 12 passed in 1.02s ====="
FAILING = (
    "FAILED tests/test_db.py::test_rows - AssertionError: boom\n"
    "===== 1 failed, 11 passed in 1.02s ====="
)


class StubContainer:
    def __init__(self, exit_code: int, stdout: str, *, hang: bool = False) -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._hang = hang
        self.killed = False
        self.removed = False

    def wait(self, timeout=None):
        if self._hang:
            raise TimeoutError("container did not finish")
        return {"StatusCode": self._exit_code}

    def logs(self, stdout=True, stderr=False):
        return (self._stdout if stdout else "").encode()

    def kill(self):
        self.killed = True

    def remove(self, force=False):
        self.removed = True


class StubContainers:
    def __init__(self, container: StubContainer) -> None:
        self._container = container
        self.run_kwargs: dict = {}

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return self._container


class StubDocker:
    def __init__(self, exit_code: int = 0, stdout: str = PASSING, hang: bool = False) -> None:
        self.container = StubContainer(exit_code, stdout, hang=hang)
        self.containers = StubContainers(self.container)

    def ping(self):
        return True


@pytest.fixture
def snapshot(tmp_path) -> RepositorySnapshot:
    return RepositorySnapshot(
        repository=REPOSITORY, local_path=tmp_path, head_sha="a" * 40,
        languages=("Python",), test_runner=TestRunner.PYTEST,
        test_command=("python", "-m", "pytest", "-q"),
        lint_command=("python", "-m", "ruff", "check", "."),
        test_files=("tests/test_db.py",), source_files=("src/db.py",),
    )


# --- the security controls --------------------------------------------------


def test_the_container_is_created_with_every_control(tmp_path, snapshot):
    """Each of these closes a specific capability. A refactor dropping one would
    otherwise be invisible, so they are asserted individually."""
    docker = StubDocker()
    sandbox = DockerSandbox(client=docker)

    sandbox.run_checks("img", tmp_path, snapshot, include_lint=False)

    kwargs = docker.containers.run_kwargs
    assert kwargs["network_disabled"] is True, "no egress: the most valuable control"
    assert kwargs["read_only"] is True
    assert "/tmp" in kwargs["tmpfs"]
    assert kwargs["mem_limit"] == kwargs["memswap_limit"], "no escape via swap"
    assert kwargs["pids_limit"] > 0, "a fork bomb must hit a ceiling"
    assert kwargs["nano_cpus"] > 0
    assert kwargs["cap_drop"] == ["ALL"]
    assert "no-new-privileges" in kwargs["security_opt"]
    assert kwargs["user"].startswith("10001"), "not root"


def test_the_working_copy_is_the_only_thing_mounted(tmp_path, snapshot):
    docker = StubDocker()

    DockerSandbox(client=docker).run_checks("img", tmp_path, snapshot, include_lint=False)

    volumes = docker.containers.run_kwargs["volumes"]
    assert list(volumes) == [str(tmp_path)]
    assert volumes[str(tmp_path)]["bind"] == "/repo"


def test_the_container_is_always_removed(tmp_path, snapshot):
    """Containers accumulate and consume disk. Cleanup is in a `finally`."""
    docker = StubDocker()

    DockerSandbox(client=docker).run_checks("img", tmp_path, snapshot, include_lint=False)

    assert docker.container.removed


# --- interpreting results ---------------------------------------------------


def test_a_passing_run_is_a_pass(tmp_path, snapshot):
    result = DockerSandbox(client=StubDocker(0, PASSING)).run_checks(
        "img", tmp_path, snapshot, include_lint=False
    )

    assert result.passed
    assert result.tests.outcome is CheckOutcome.PASSED
    assert result.tests.passed_count == 12


def test_a_failing_run_reports_the_specific_failures(tmp_path, snapshot):
    result = DockerSandbox(client=StubDocker(1, FAILING)).run_checks(
        "img", tmp_path, snapshot, include_lint=False
    )

    assert not result.passed
    assert result.tests.failed_count == 1
    assert "test_rows" in result.failures[0].test_id


def test_the_exit_code_decides_not_the_parser(tmp_path, snapshot):
    """Output formats vary between versions and a parser can be wrong. A
    non-zero exit code means the tool failed regardless of what it printed."""
    docker = StubDocker(exit_code=1, stdout=PASSING)  # says passed, exited non-zero

    result = DockerSandbox(client=docker).run_checks(
        "img", tmp_path, snapshot, include_lint=False
    )

    assert result.tests.outcome is CheckOutcome.FAILED
    assert not result.passed


def test_no_tests_collected_is_an_error_not_a_pass(tmp_path, snapshot):
    """pytest exit code 5. Nothing was verified, so it must not read as success —
    otherwise a patch that breaks collection looks validated."""
    result = DockerSandbox(client=StubDocker(5, "no tests ran")).run_checks(
        "img", tmp_path, snapshot, include_lint=False
    )

    assert result.tests.outcome is CheckOutcome.ERROR
    assert not result.passed


def test_a_hanging_container_is_killed_and_reported(tmp_path, snapshot):
    """Docker has no run timeout of its own, so it is enforced from outside."""
    docker = StubDocker(hang=True)

    result = DockerSandbox(client=docker, timeout_s=0.1).run_checks(
        "img", tmp_path, snapshot, include_lint=False
    )

    assert docker.container.killed
    assert result.tests.outcome is CheckOutcome.TIMEOUT
    assert not result.passed


def test_a_missing_command_is_skipped_not_failed(tmp_path, snapshot):
    """A repository without a linter has not failed linting. Conflating absent
    with failed would block every patch to a project that does not use one."""
    without_lint = snapshot.model_copy(update={"lint_command": None})

    result = DockerSandbox(client=StubDocker()).run_checks(
        "img", tmp_path, without_lint, include_lint=True
    )

    lint = next(c for c in result.checks if c.name == "lint")
    assert lint.outcome is CheckOutcome.SKIPPED
    assert lint.ok
    assert result.passed, "a skipped optional check must not block a good patch"


def test_a_sandbox_failure_is_not_a_pass(tmp_path, snapshot):
    """'We could not tell' and 'it works' must never be the same value, or a
    broken sandbox silently approves patches."""

    class BrokenDocker(StubDocker):
        def __init__(self):
            super().__init__()
            self.containers = self

        def run(self, **kwargs):
            raise RuntimeError("daemon went away")

    result = DockerSandbox(client=BrokenDocker()).run_checks(
        "img", tmp_path, snapshot, include_lint=False
    )

    assert result.sandbox_error is not None
    assert not result.passed


def test_an_unreachable_daemon_is_reported_actionably(monkeypatch):
    """The message has to say what to do. 'ConnectionError' does not."""
    import docker as docker_module

    def refuse():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(docker_module, "from_env", refuse)

    with pytest.raises(SandboxError, match="Docker Desktop"):
        DockerSandbox()._docker()


def test_availability_can_be_checked_without_raising(monkeypatch):
    """The CLI needs to say 'Docker is not running' rather than crash."""
    import docker as docker_module

    monkeypatch.setattr(
        docker_module, "from_env", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )

    assert DockerSandbox().available() is False


# --- feedback for the debug loop --------------------------------------------


def test_repair_feedback_names_the_failing_tests(tmp_path, snapshot):
    result = DockerSandbox(client=StubDocker(1, FAILING)).run_checks(
        "img", tmp_path, snapshot, include_lint=False
    )

    feedback = result.feedback_for_repair()

    assert "test_rows" in feedback
    assert "AssertionError" in feedback


def test_repair_feedback_is_bounded(tmp_path, snapshot):
    """Twenty near-identical failures are usually one bug, and sending all of
    them costs tokens while hiding the signal."""
    many = "\n".join(f"FAILED tests/a.py::test_{i} - boom" for i in range(30))
    result = DockerSandbox(client=StubDocker(1, many + "\n==== 30 failed in 1s ====")).run_checks(
        "img", tmp_path, snapshot, include_lint=False
    )

    feedback = result.feedback_for_repair(max_failures=5)

    assert "and 25 more" in feedback
    assert feedback.count("test_") <= 8


def test_repair_feedback_explains_a_sandbox_error(tmp_path, snapshot):
    from patchpilot.models import ValidationResult

    feedback = ValidationResult(sandbox_error="daemon down").feedback_for_repair()

    assert "could not run" in feedback
