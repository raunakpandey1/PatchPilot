"""Exception hierarchy.

Why a hierarchy rather than raising ``Exception`` everywhere: callers need to
react differently to different failures. A rate limit is worth waiting for. A
missing repository is not — retrying it forever just burns time. Typed errors
let the caller express that difference without matching on error strings.
"""

from __future__ import annotations

from datetime import datetime


class PatchPilotError(Exception):
    """Base class. Catching this catches everything we raise deliberately."""


# --- GitHub ---------------------------------------------------------------


class GitHubError(PatchPilotError):
    """Something went wrong talking to GitHub."""


class RepositoryNotFound(GitHubError):
    def __init__(self, full_name: str) -> None:
        super().__init__(
            f"Repository {full_name!r} not found. It may be private, renamed, "
            f"or deleted — or the name may be misspelled."
        )
        self.full_name = full_name


class IssueNotFound(GitHubError):
    def __init__(self, full_name: str, number: int) -> None:
        super().__init__(f"Issue #{number} not found in {full_name!r}.")
        self.full_name = full_name
        self.number = number


class AuthenticationError(GitHubError):
    """The token was rejected. Retrying will not help."""


class RateLimitExceeded(GitHubError):
    """The request budget is spent.

    Carries ``reset_at`` so the caller can decide: wait, or give up and report
    honestly. We never silently sleep for an unknown length of time.
    """

    def __init__(self, reset_at: datetime | None, limit: int | None = None) -> None:
        when = reset_at.isoformat() if reset_at else "an unknown time"
        super().__init__(f"GitHub rate limit exhausted (limit={limit}). Resets at {when}.")
        self.reset_at = reset_at
        self.limit = limit


# --- Git ------------------------------------------------------------------


class GitError(PatchPilotError):
    """A git command failed."""


class CloneFailed(GitError):
    def __init__(self, url: str, stderr: str) -> None:
        super().__init__(f"Failed to clone {url}: {stderr.strip()[:500]}")
        self.url = url
        self.stderr = stderr


class CloneTimeout(GitError):
    """The clone exceeded its time budget — a repository may be huge or hostile."""

    def __init__(self, url: str, timeout_s: float) -> None:
        super().__init__(f"Clone of {url} exceeded {timeout_s}s budget and was killed.")
        self.url = url
        self.timeout_s = timeout_s


class RepositoryTooLarge(GitError):
    """The repository exceeded its disk budget. Treated as hostile input."""

    def __init__(self, url: str, size_mb: float, limit_mb: int) -> None:
        super().__init__(
            f"{url} is {size_mb:.0f} MB, over the {limit_mb} MB budget. Refusing to clone."
        )
        self.url = url
        self.size_mb = size_mb
        self.limit_mb = limit_mb


# --- Workspace ------------------------------------------------------------


class WorkspaceError(PatchPilotError):
    """A path operation tried to leave the workspace.

    This is a security boundary, not a convenience check: repository content is
    untrusted, and a symlink or ``../`` in a path must never reach the host
    filesystem.
    """


# --- Analysis -------------------------------------------------------------


class AnalysisError(PatchPilotError):
    """The repository could not be characterised."""
