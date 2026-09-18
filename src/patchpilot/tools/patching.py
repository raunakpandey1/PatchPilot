"""Applying a proposed patch — to a copy, never to the original.

Two rules, both non-negotiable
------------------------------
**The clone is never modified.** Every patch is applied to a fresh copy. The
original clone stays pristine so a failed attempt costs nothing to recover from,
and so several attempts in the Phase 7 debug loop start from the same known
state rather than accumulating each other's damage.

**Every write goes through the workspace.** ``file_path`` comes from a language
model, which was reading a repository written by a stranger. ``../../.ssh/id_rsa``
is a valid string. See :mod:`patchpilot.tools.workspace`.

Why exact-text edits rather than a model-written diff
-----------------------------------------------------
A unified diff hunk header looks like ``@@ -40,7 +40,9 @@`` — start line and
line count, before and after. Getting it right requires counting context lines
correctly, and models are unreliable at exactly that kind of arithmetic. When
they get it wrong, ``git apply`` fails with a message about corrupt patches,
which tells you nothing about whether the *fix* was right.

Asking instead for the exact text to replace removes the arithmetic entirely.
The model reproduces a snippet it was just shown, which is something it is good
at. Verification becomes a string search with three possible outcomes — found
once, not found, found many times — each detectable in code with a message
precise enough for the debug loop to act on.

The real unified diff is then generated here, from the before and after states,
so it is correct by construction rather than by hope.
"""

from __future__ import annotations

import difflib
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from patchpilot.errors import PatchPilotError
from patchpilot.logging import get_logger
from patchpilot.models import CodeEdit, Patch
from patchpilot.tools.workspace import Workspace

log = get_logger("patching")

# Directories never copied into a working copy: version control internals, caches
# and virtual environments. Copying `.git` would also make the copy enormous.
EXCLUDED_FROM_COPY = shutil.ignore_patterns(
    ".git", "__pycache__", ".venv", "node_modules", ".pytest_cache", ".mypy_cache", "*.pyc"
)


class PatchError(PatchPilotError):
    """A patch could not be applied. The message is written for the debug loop."""


@dataclass
class EditResult:
    """What happened to one edit."""

    edit: CodeEdit
    applied: bool
    error: str | None = None


@dataclass
class PatchResult:
    """The outcome of applying a whole patch."""

    patch: Patch
    workspace_path: Path
    results: list[EditResult] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        return bool(self.results) and all(r.applied for r in self.results)

    @property
    def failures(self) -> list[EditResult]:
        return [r for r in self.results if not r.applied]

    def failure_report(self) -> str:
        """Feedback for the debug loop.

        Deliberately specific. "Patch failed" gives a model nothing to work
        with; "old_text not found in db.py — the file may have changed, or the
        snippet was not copied exactly" tells it what to do differently.
        """
        if self.applied:
            return "all edits applied"
        return "\n".join(
            f"{r.edit.file_path}: {r.error}" for r in self.failures
        )


def create_working_copy(
    workspace: Workspace, source: Path, name: str, *, replace: bool = True
) -> Path:
    """Copy a repository into an isolated directory inside the workspace.

    ``.git`` is excluded: the copy is for editing and testing, not for history,
    and copying it would multiply the size for no benefit. Phase 9 creates a
    branch in the *original* clone once a human approves.
    """
    destination = workspace.resolve(Path("attempts") / name)
    if destination.exists():
        if not replace:
            raise PatchError(f"working copy {destination} already exists")
        shutil.rmtree(destination, ignore_errors=True)

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=EXCLUDED_FROM_COPY, symlinks=False)

    log.info("working_copy_created", source=str(source), destination=str(destination))
    return destination


def apply_edits(
    workspace: Workspace, root: Path, edits: Sequence[CodeEdit]
) -> tuple[list[EditResult], dict[str, tuple[str, str]]]:
    """Apply exact-text edits, returning per-edit results and before/after text.

    Edits are applied in order and each is checked independently. A later edit
    can legitimately depend on an earlier one, so the file is re-read each time
    rather than snapshotted up front.
    """
    results: list[EditResult] = []
    originals: dict[str, str] = {}
    changed: dict[str, tuple[str, str]] = {}

    for edit in edits:
        try:
            # Confinement check before any filesystem access. The path came from
            # a model reading untrusted content.
            path = workspace.resolve(root / edit.file_path)
        except PatchPilotError as exc:
            results.append(EditResult(edit, False, f"path rejected: {exc}"))
            continue

        if not path.is_file():
            results.append(
                EditResult(edit, False, f"file does not exist: {edit.file_path}")
            )
            continue

        content = path.read_text(encoding="utf-8", errors="replace")
        originals.setdefault(edit.file_path, content)

        occurrences = content.count(edit.old_text)
        if occurrences == 0:
            results.append(
                EditResult(
                    edit, False,
                    "old_text was not found. Copy the existing code exactly, "
                    "including indentation and whitespace.",
                )
            )
            continue
        if occurrences > 1:
            results.append(
                EditResult(
                    edit, False,
                    f"old_text appears {occurrences} times and is ambiguous. "
                    f"Include more surrounding lines so it matches exactly once.",
                )
            )
            continue

        path.write_text(content.replace(edit.old_text, edit.new_text, 1), encoding="utf-8")
        results.append(EditResult(edit, True))

    for file_path, before in originals.items():
        after = (root / file_path).read_text(encoding="utf-8", errors="replace")
        if before != after:
            changed[file_path] = (before, after)

    return results, changed


def build_patch(
    edits: Sequence[CodeEdit],
    changed: dict[str, tuple[str, str]],
    *,
    explanation: str = "",
) -> Patch:
    """Compute a real unified diff from the before and after contents.

    Generated here rather than asked for, so the hunk headers are arithmetically
    correct by construction. This is the half of the design that lets the model
    avoid line numbers entirely.
    """
    diffs: list[str] = []
    for file_path in sorted(changed):
        before, after = changed[file_path]
        diffs.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                n=3,
            )
        )

    return Patch(
        edits=tuple(edits),
        diff="".join(diffs),
        files_changed=tuple(sorted(changed)),
        explanation=explanation,
    )


def apply_patch(
    workspace: Workspace,
    source_repo: Path,
    edits: Sequence[CodeEdit],
    *,
    attempt_name: str,
    explanation: str = "",
) -> PatchResult:
    """Copy the repository, apply the edits, and produce the resulting diff."""
    working_copy = create_working_copy(workspace, source_repo, attempt_name)
    results, changed = apply_edits(workspace, working_copy, edits)
    patch = build_patch(edits, changed, explanation=explanation)

    log.info(
        "patch_applied",
        attempt=attempt_name,
        edits=len(edits),
        applied=sum(1 for r in results if r.applied),
        files_changed=len(changed),
        lines_added=patch.lines_added,
        lines_removed=patch.lines_removed,
    )
    return PatchResult(patch=patch, workspace_path=working_copy, results=results)
