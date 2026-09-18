"""Git operations, via subprocess.

Why subprocess rather than GitPython
------------------------------------
Three reasons, all practical:

* **Timeouts.** ``subprocess.run(timeout=...)`` kills a hung clone. A hostile or
  merely enormous repository must not be able to block forever, and library
  wrappers make that control awkward.
* **Exact flags.** We need ``--filter=blob:none`` and a specific set of
  hardening options. Passing them through a wrapper is more work than calling
  git directly.
* **Legibility.** The commands in this file are the commands you would type.
  Anyone reading it learns git, not a library's opinion about git.

Clone strategies
----------------
A git repository is a content-addressed store of *objects*: blobs (file
contents), trees (directories), and commits. Cloning normally downloads all of
them for all of history — which for an old repository is mostly file versions
nobody will ever look at.

===========  ==========================  ==============================
Strategy     What it downloads           What it costs you
===========  ==========================  ==============================
FULL         everything                  slow, large
SHALLOW      one commit, its files       **no history** — no ``log``, no
             (``--depth=1``)             ``blame``, no "when did this break"
BLOBLESS     all commits and trees,      one extra fetch the first time you
             file contents on demand     read an old file version
             (``--filter=blob:none``)
===========  ==========================  ==============================

PatchPilot uses **BLOBLESS**. Phase 4 needs ``git log`` and ``git blame`` to
find when a bug was introduced, which rules out shallow; and it does not need
every historical version of every file, which rules out full.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from patchpilot.errors import CloneFailed, CloneTimeout, GitError, RepositoryTooLarge
from patchpilot.logging import get_logger
from patchpilot.models import Commit

log = get_logger("git")


class CloneStrategy(StrEnum):
    FULL = "full"
    SHALLOW = "shallow"
    BLOBLESS = "blobless"


def _hardened_env(home: Path) -> dict[str, str]:
    """Environment for every git call we make.

    Each variable closes a specific hole:

    * ``GIT_TERMINAL_PROMPT=0`` — a URL for a private repo would otherwise make
      git block forever waiting for a username at a terminal nobody is watching.
    * ``GIT_ASKPASS`` / ``SSH_ASKPASS`` pointing at ``false`` — same, for the
      GUI credential prompt.
    * ``GIT_CONFIG_NOSYSTEM`` and a throwaway ``HOME`` — git will not read the
      developer's global config, aliases, or **credential helper**. Without
      this, cloning a hostile URL could hand over stored credentials.
    * ``GIT_LFS_SKIP_SMUDGE`` — do not download Git-LFS payloads, which can be
      gigabytes and are never what we need.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/usr/bin/false",
        "SSH_ASKPASS": "/usr/bin/false",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "LC_ALL": "C",  # stable, parseable output regardless of locale
    }
    return env


def _run_git(
    args: Sequence[str],
    *,
    cwd: Path,
    timeout_s: float,
    home: Path | None = None,
) -> str:
    """Run one git command and return stdout, or raise ``GitError``."""
    command = ["git", *args]
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=_hardened_env(home or cwd),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} exceeded {timeout_s}s") from exc

    elapsed = time.monotonic() - started
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed ({result.returncode}): {result.stderr.strip()[:400]}")

    log.debug("git_command", args=" ".join(args[:3]), duration_s=round(elapsed, 3))
    return result.stdout


def clone(
    url: str,
    destination: Path,
    *,
    strategy: CloneStrategy = CloneStrategy.BLOBLESS,
    branch: str | None = None,
    timeout_s: float = 300.0,
    max_size_mb: int | None = 500,
    allow_file_protocol: bool = False,
) -> GitRepository:
    """Clone ``url`` into ``destination``.

    The repository is untrusted input, so this call is budgeted: it cannot run
    longer than ``timeout_s`` and the result cannot exceed ``max_size_mb``.
    Both are enforced, not hoped for.

    Submodules are never recursed. A ``.gitmodules`` file is attacker-controlled
    text that says "also fetch code from this other URL" — following it means
    fetching from wherever a stranger points.

    ``allow_file_protocol`` defaults to False, which blocks git's ``file``
    transport. Real work always clones over HTTPS, so a ``file://`` or local
    path URL is either a mistake or an attempt to make us read the local disk.
    Tests set it to True to clone from a fixture repository on disk — the one
    legitimate use, made explicit at the call site rather than by weakening the
    default.
    """
    destination = destination.resolve()
    if destination.exists():
        raise GitError(f"{destination} already exists. Remove it or reuse the existing clone.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    args = [
        # Block the file:// transport, which submodules and some URLs can use
        # to read the local filesystem.
        "-c", f"protocol.file.allow={'always' if allow_file_protocol else 'never'}",
        "clone",
        "--no-tags",           # tags are rarely useful here and cost transfer
        "--single-branch",     # one branch, not every branch ever pushed
    ]
    if strategy is CloneStrategy.SHALLOW:
        args += ["--depth", "1"]
    elif strategy is CloneStrategy.BLOBLESS:
        args += ["--filter=blob:none"]
    if branch:
        args += ["--branch", branch]
    args += [url, str(destination)]

    started = time.monotonic()
    try:
        _run_git(args, cwd=destination.parent, timeout_s=timeout_s, home=destination.parent)
    except GitError as exc:
        shutil.rmtree(destination, ignore_errors=True)
        if "exceeded" in str(exc):
            raise CloneTimeout(url, timeout_s) from exc
        raise CloneFailed(url, str(exc)) from exc

    elapsed = time.monotonic() - started
    size_mb = _directory_size_mb(destination)

    if max_size_mb is not None and size_mb > max_size_mb:
        # Enforced *after* the clone because git gives no reliable way to abort
        # partway on size. The timeout is the real defence against a repository
        # that is pathologically large; this catches the merely-too-big.
        shutil.rmtree(destination, ignore_errors=True)
        raise RepositoryTooLarge(url, size_mb, max_size_mb)

    log.info(
        "repository_cloned",
        url=url,
        strategy=str(strategy),
        duration_s=round(elapsed, 2),
        size_mb=round(size_mb, 1),
    )
    return GitRepository(destination, clone_duration_s=elapsed, size_mb=size_mb)


def _directory_size_mb(path: Path) -> float:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
    return total / (1024 * 1024)


class GitRepository:
    """A local clone. Read-only in Phase 1; writes arrive in Phase 5."""

    def __init__(
        self,
        path: Path,
        *,
        clone_duration_s: float | None = None,
        size_mb: float | None = None,
        command_timeout_s: float = 60.0,
    ) -> None:
        self.path = path.resolve()
        self.clone_duration_s = clone_duration_s
        self.size_mb = size_mb
        self._timeout = command_timeout_s
        if not (self.path / ".git").exists():
            raise GitError(f"{self.path} is not a git repository.")

    def _git(self, *args: str) -> str:
        return _run_git(args, cwd=self.path, timeout_s=self._timeout, home=self.path.parent)

    # --- state -------------------------------------------------------------

    @property
    def head_sha(self) -> str:
        """The exact commit we are looking at.

        Recorded in every snapshot so a benchmark result can be reproduced
        against the same code, not against whatever the branch has become.
        """
        return self._git("rev-parse", "HEAD").strip()

    @property
    def current_branch(self) -> str:
        return self._git("rev-parse", "--abbrev-ref", "HEAD").strip()

    # --- history -----------------------------------------------------------

    def log(self, *, max_count: int = 50, paths: Sequence[str] | None = None) -> list[Commit]:
        """Recent commits, newest first.

        The format string uses ``%x1f`` (unit separator) between fields and
        ``%x1e`` (record separator) between commits. Commit subjects contain
        every printable character a human can type — splitting on a comma or a
        pipe breaks on real data. These two control characters do not appear in
        commit messages.
        """
        fmt = "%H%x1f%an%x1f%aI%x1f%s%x1e"
        args = ["log", f"--max-count={max_count}", f"--format={fmt}"]
        if paths:
            args += ["--", *paths]

        raw = self._git(*args)
        commits: list[Commit] = []
        for record in raw.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, author, authored, subject = record.split("\x1f")
            commits.append(
                Commit(
                    sha=sha,
                    author=author,
                    authored_at=datetime.fromisoformat(authored),
                    subject=subject,
                )
            )
        return commits

    def files_changed_in(self, sha: str) -> tuple[str, ...]:
        """Which files a commit touched.

        This is the key to free ground truth: for a closed issue, the commit
        that fixed it names exactly the files that needed to change. That gives
        us a labelled dataset for measuring retrieval in Phase 3, without
        anyone hand-annotating anything.
        """
        raw = self._git("show", "--name-only", "--format=", "--no-renames", sha)
        return tuple(line.strip() for line in raw.splitlines() if line.strip())

    def blame_line(self, file_path: str, line: int) -> str | None:
        """Which commit last touched one line. ``None`` if the line is unknown."""
        try:
            raw = self._git("blame", "-L", f"{line},{line}", "--porcelain", file_path)
        except GitError:
            return None
        return raw.split()[0] if raw else None

    def diff(self, ref_a: str, ref_b: str = "HEAD") -> str:
        return self._git("diff", ref_a, ref_b)

    def show_file(self, sha: str, file_path: str) -> str:
        """Contents of a file at a particular commit."""
        return self._git("show", f"{sha}:{file_path}")

    def list_files(self) -> tuple[str, ...]:
        """Every tracked file.

        ``git ls-files`` rather than walking the directory: it returns exactly
        what git tracks, skipping ``.git`` internals, ignored build output and
        anything untracked that happens to be lying around.
        """
        raw = self._git("ls-files")
        return tuple(line for line in raw.splitlines() if line)
