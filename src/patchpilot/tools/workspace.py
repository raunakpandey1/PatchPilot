"""The workspace — one directory that all untrusted content stays inside.

Why this exists
---------------
PatchPilot clones repositories written by strangers. A repository can contain:

* a symlink named ``config.py`` pointing at ``/Users/you/.ssh/id_rsa``
* a file path like ``../../../../etc/passwd`` inside a patch
* a path that looks harmless but resolves somewhere else entirely

If any later phase reads or writes a path without checking, that content
escapes onto the host filesystem. So every filesystem access in PatchPilot goes
through a ``Workspace``, which resolves the path fully — following symlinks —
and refuses anything that lands outside its root.

This is a security boundary, and it is deliberately boring: a boundary with
clever exceptions is not a boundary.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from patchpilot.errors import WorkspaceError
from patchpilot.logging import get_logger

log = get_logger("workspace")


class Workspace:
    """A rooted directory. Nothing escapes it."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def __repr__(self) -> str:
        return f"Workspace({self.root})"

    def resolve(self, candidate: Path | str) -> Path:
        """Resolve a path and prove it stays inside the workspace.

        ``strict=False`` so a path that does not exist yet (a file we are about
        to write) still resolves. ``resolve()`` follows symlinks, which is the
        whole point — a symlink pointing outside is caught here.
        """
        path = Path(candidate)
        absolute = path if path.is_absolute() else self.root / path
        resolved = absolute.resolve()

        if resolved != self.root and not resolved.is_relative_to(self.root):
            log.warning("workspace_escape_blocked", attempted=str(candidate), resolved=str(resolved))
            raise WorkspaceError(
                f"Path {candidate!r} resolves to {resolved}, which is outside "
                f"the workspace at {self.root}. Refusing."
            )
        return resolved

    def is_inside(self, candidate: Path | str) -> bool:
        """Non-raising version of :meth:`resolve`, for filtering."""
        try:
            self.resolve(candidate)
        except WorkspaceError:
            return False
        return True

    def repo_dir(self, full_name: str) -> Path:
        """Where a given repository's clone lives.

        ``owner/name`` becomes ``<root>/repos/owner__name`` — flattened so a
        repository called ``a/b/c`` (impossible on GitHub today, but the input
        is untrusted) cannot create nesting we did not intend.
        """
        safe = full_name.replace("/", "__").replace("..", "__")
        return self.resolve(Path("repos") / safe)

    def cache_dir(self) -> Path:
        path = self.resolve("cache")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def clear(self, path: Path | str) -> None:
        """Delete something inside the workspace. Refuses to touch the root."""
        target = self.resolve(path)
        if target == self.root:
            raise WorkspaceError("Refusing to delete the workspace root.")
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()

    def size_bytes(self, path: Path | str | None = None) -> int:
        """Recursive size on disk, for enforcing budgets."""
        target = self.resolve(path) if path is not None else self.root
        if not target.exists():
            return 0
        return sum(f.stat().st_size for f in target.rglob("*") if f.is_file() and not f.is_symlink())
