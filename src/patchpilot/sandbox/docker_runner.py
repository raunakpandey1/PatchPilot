"""Running untrusted code in a container.

The threat, stated plainly
--------------------------
To find out whether a patch works, the repository's test suite has to run. That
test suite is code written by strangers, and PatchPilot just modified it. It can
do anything the process it runs in can do: read ``~/.ssh``, exfiltrate
environment variables, delete files, mine cryptocurrency, or simply never
terminate.

Running it on the host is not an option, and "the repository looks reputable" is
not a control.

What the container actually restricts
--------------------------------------
Each of these closes a specific capability, and each is set explicitly rather
than left to Docker's defaults:

``network_disabled=True``
    No egress at all. A test cannot phone home with your environment, and cannot
    fetch a second-stage payload. This is the single most valuable control, and
    it is why dependency installation happens in a *separate, earlier* step —
    installing packages requires network **and** executes ``setup.py``, which is
    itself arbitrary code.

``read_only=True`` plus a ``tmpfs`` for ``/tmp``
    The image's filesystem cannot be modified. Writes go to the mounted working
    copy (already a disposable copy) or to memory-backed ``/tmp`` that vanishes
    with the container.

``mem_limit`` / ``pids_limit`` / ``nano_cpus``
    A fork bomb or a runaway allocation exhausts the container's allowance, not
    the laptop's. On 8 GB of RAM this is the difference between a failed test
    run and a hung machine.

``cap_drop=["ALL"]``, ``security_opt=["no-new-privileges"]``
    Even as root inside the container, no capability to mount, change network
    configuration, or escalate through a setuid binary.

``user`` set to a non-root uid
    Files created in the mount belong to an unprivileged user.

A wall-clock timeout, enforced by us
    Docker has no built-in run timeout. A test that blocks forever would
    otherwise hold the run open indefinitely, so the container is killed from
    outside.

What this does **not** protect against
---------------------------------------
A container is not a virtual machine. It shares the host kernel, so a kernel
exploit escapes it. For a project that runs tests from public repositories this
is an accepted risk, stated rather than glossed: the realistic threats here are
accidental damage and opportunistic exfiltration, both of which the controls
above do stop. A hostile party specifically targeting this system with a kernel
0-day is out of scope, and the honest mitigation would be a VM or gVisor.
"""

from __future__ import annotations

import io
import tarfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from patchpilot.errors import PatchPilotError
from patchpilot.logging import get_logger
from patchpilot.models import (
    CheckOutcome,
    CheckResult,
    RepositorySnapshot,
    ValidationResult,
)
from patchpilot.sandbox.parsing import parse_lint, parse_pytest, parse_unittest, tail

log = get_logger("sandbox")

DEFAULT_BASE_IMAGE = "python:3.13-slim"

# Deliberately modest. The whole point is that a runaway test cannot take the
# machine with it, and this machine has 8 GB total with Docker Desktop already
# holding a share of it.
DEFAULT_MEMORY = "2g"
DEFAULT_CPUS = 2.0
DEFAULT_PIDS = 512
DEFAULT_TIMEOUT_S = 300.0


class SandboxError(PatchPilotError):
    """The sandbox itself failed. Distinct from "the tests failed"."""


class DockerSandbox:
    """Runs a repository's checks inside a locked-down container."""

    def __init__(
        self,
        *,
        base_image: str = DEFAULT_BASE_IMAGE,
        memory: str = DEFAULT_MEMORY,
        cpus: float = DEFAULT_CPUS,
        pids_limit: int = DEFAULT_PIDS,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        client: Any | None = None,
    ) -> None:
        self._base_image = base_image
        self._memory = memory
        self._cpus = cpus
        self._pids = pids_limit
        self._timeout = timeout_s
        self._client: Any = client  # injectable, so unit tests need no daemon

    # --- client ------------------------------------------------------------

    def _docker(self) -> Any:
        if self._client is None:
            import docker

            try:
                self._client = docker.from_env()
                self._client.ping()
            except Exception as exc:  # docker not installed, or daemon down
                raise SandboxError(
                    f"cannot reach the Docker daemon ({exc}). "
                    f"Start Docker Desktop, or run with --no-sandbox to skip validation."
                ) from exc
        return self._client

    def available(self) -> bool:
        try:
            self._docker()
        except SandboxError:
            return False
        return True

    # --- environment preparation -------------------------------------------

    def prepare_image(self, snapshot: RepositorySnapshot, *, tag: str) -> str:
        """Build an image with the repository's dependencies installed.

        **This step has network access, and that is the point of separating it.**
        Installing dependencies requires the network *and* runs arbitrary code —
        ``setup.py`` executes, and so do any build hooks. So it happens once,
        here, in its own container, and the image it produces is then reused for
        test runs that have no network at all.

        Treating "install" and "run" as the same trust level is the mistake this
        split exists to avoid.
        """
        client = self._docker()
        dockerfile = self._dockerfile_for(snapshot)

        context = io.BytesIO()
        with tarfile.open(fileobj=context, mode="w") as archive:
            data = dockerfile.encode()
            info = tarfile.TarInfo("Dockerfile")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
            archive.add(snapshot.local_path, arcname="repo", recursive=True)
        context.seek(0)

        started = time.monotonic()
        try:
            # Default networking, deliberately: this step installs dependencies
            # and needs egress. It is the only step that has it.
            image, _ = client.images.build(
                fileobj=context, custom_context=True, tag=tag, rm=True, forcerm=True,
            )
        except Exception as exc:
            raise SandboxError(f"image build failed: {str(exc)[:500]}") from exc

        log.info("sandbox_image_built", tag=tag, seconds=round(time.monotonic() - started, 1))
        return str(image.tags[0] if image.tags else image.id)

    def _dockerfile_for(self, snapshot: RepositorySnapshot) -> str:
        """The build recipe.

        Best-effort dependency installation: a repository may declare its
        dependencies in any of several ways, and one failing must not abort the
        build — the tests will report the real problem more usefully than a
        build error would.
        """
        return f"""\
FROM {self._base_image}

# A non-root user, created at build time so the runtime user exists in the
# image's passwd file — some tools refuse to run as an unknown uid.
RUN useradd --create-home --uid 10001 sandbox

WORKDIR /repo
COPY repo/ /repo/

# Best effort: install whichever dependency declaration this repository uses.
# `|| true` because a failed install should surface as a failing test, not as a
# build error that hides which dependency was missing.
RUN pip install --no-cache-dir --upgrade pip \\
 && (pip install --no-cache-dir -e ".[test,dev]" \\
     || pip install --no-cache-dir -e . \\
     || pip install --no-cache-dir -r requirements.txt \\
     || true) \\
 && pip install --no-cache-dir pytest || true

RUN chown -R sandbox:sandbox /repo
USER sandbox
"""

    # --- running checks ----------------------------------------------------

    def run_checks(
        self,
        image: str,
        working_copy: Path,
        snapshot: RepositorySnapshot,
        *,
        include_lint: bool = True,
        include_typecheck: bool = False,
    ) -> ValidationResult:
        """Run the repository's own checks against a patched working copy."""
        checks: list[CheckResult] = []

        planned: list[tuple[str, tuple[str, ...] | None]] = [
            ("tests", snapshot.test_command)
        ]
        if include_lint:
            planned.append(("lint", snapshot.lint_command))
        if include_typecheck:
            planned.append(("typecheck", snapshot.typecheck_command))

        for name, command in planned:
            if command is None:
                # Absent is not failed. A repository without a linter has not
                # failed linting, and treating it as a failure would block every
                # patch to a project that does not use one.
                checks.append(
                    CheckResult(name=name, command=(), outcome=CheckOutcome.SKIPPED)
                )
                continue
            try:
                checks.append(self._run_one(image, working_copy, name, command))
            except SandboxError as exc:
                return ValidationResult(
                    checks=tuple(checks), image=image, sandbox_error=str(exc)
                )

        return ValidationResult(checks=tuple(checks), image=image)

    def _run_one(
        self, image: str, working_copy: Path, name: str, command: Sequence[str]
    ) -> CheckResult:
        client = self._docker()
        started = time.monotonic()
        container = None

        try:
            container = client.containers.run(
                image=image,
                command=list(command),
                working_dir="/repo",
                # The patched copy replaces the image's copy of the repository.
                volumes={str(working_copy): {"bind": "/repo", "mode": "rw"}},
                # --- the controls ---
                network_disabled=True,
                read_only=True,
                tmpfs={"/tmp": "rw,size=256m,noexec"},
                mem_limit=self._memory,
                memswap_limit=self._memory,   # equal to mem_limit: no swap escape
                nano_cpus=int(self._cpus * 1_000_000_000),
                pids_limit=self._pids,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                user="10001:10001",
                environment={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"},
                detach=True,
            )

            try:
                status = container.wait(timeout=self._timeout)
                exit_code = int(status.get("StatusCode", -1))
                outcome_timeout = False
            except Exception:
                # Docker provides no run timeout of its own, so the container is
                # killed from outside. A test that blocks forever would
                # otherwise hold the whole run open.
                container.kill()
                exit_code, outcome_timeout = -1, True

            stdout = container.logs(stdout=True, stderr=False).decode(errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode(errors="replace")

        except SandboxError:
            raise
        except Exception as exc:
            raise SandboxError(f"running {name} failed: {str(exc)[:300]}") from exc
        finally:
            if container is not None:
                try:
                    container.remove(force=True)
                except Exception:
                    log.warning("container_cleanup_failed", check=name)

        duration = time.monotonic() - started
        return self._interpret(
            name, tuple(command), exit_code, outcome_timeout, stdout, stderr, duration
        )

    def _interpret(
        self,
        name: str,
        command: tuple[str, ...],
        exit_code: int,
        timed_out: bool,
        stdout: str,
        stderr: str,
        duration: float,
    ) -> CheckResult:
        """Turn raw output into a verdict.

        The **exit code decides** the outcome; parsing only enriches it. Output
        formats vary between versions and a parser can be wrong, but a non-zero
        exit code means the tool failed regardless of what it printed.
        """
        combined = f"{stdout}\n{stderr}"

        if timed_out:
            return CheckResult(
                name=name, command=command, outcome=CheckOutcome.TIMEOUT,
                exit_code=None, duration_s=duration,
                stdout_tail=tail(stdout), stderr_tail=tail(stderr),
            )

        if name == "tests":
            if "pytest" in " ".join(command):
                passed, failed, failures = parse_pytest(combined)
            else:
                passed, failed, failures = parse_unittest(combined)
        else:
            failed, failures = parse_lint(combined)
            passed = 0

        outcome = CheckOutcome.PASSED if exit_code == 0 else CheckOutcome.FAILED
        # Exit code 5 from pytest means "no tests collected" — the checks did not
        # actually verify anything, so it must not read as a pass.
        if name == "tests" and exit_code == 5:
            outcome = CheckOutcome.ERROR

        result = CheckResult(
            name=name, command=command, outcome=outcome, exit_code=exit_code,
            duration_s=round(duration, 2), passed_count=passed, failed_count=failed,
            failures=failures, stdout_tail=tail(stdout), stderr_tail=tail(stderr),
        )
        log.info(
            "sandbox_check",
            check=name, outcome=str(outcome), exit_code=exit_code,
            passed=passed, failed=failed, seconds=round(duration, 1),
        )
        return result
