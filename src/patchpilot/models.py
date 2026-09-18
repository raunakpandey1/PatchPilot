"""Domain models.

These are the vocabulary of the whole system. Every later phase — RAG, the
agent graph, the sandbox, the benchmark — speaks in these types rather than in
raw GitHub JSON.

Why not just pass dictionaries around? Three reasons:

1. A typo in ``issue["titel"]`` fails at 2am in the debug loop. A typo in
   ``issue.titel`` fails immediately, in your editor.
2. GitHub's JSON has ~80 fields per issue. We keep the ~12 we use, so the rest
   cannot quietly become load-bearing.
3. These objects get serialised into agent state and checkpointed to disk. A
   declared schema is what makes that safe to reload later.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field


class Frozen(BaseModel):
    """Base for immutable value objects.

    Frozen on purpose: these travel through a graph where several nodes hold a
    reference to the same object. If any node could mutate one, a bug in the
    debug loop would silently change what the reviewer node already saw.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# --- GitHub -----------------------------------------------------------------


class IssueState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class Label(Frozen):
    name: str
    description: str | None = None


class Repository(Frozen):
    """A GitHub repository, as GitHub describes it."""

    full_name: str  # "simonw/sqlite-utils"
    owner: str
    name: str
    default_branch: str
    clone_url: str
    html_url: str
    description: str | None = None
    primary_language: str | None = None
    stars: int = 0
    open_issues_count: int = 0
    size_kb: int = 0  # GitHub's own estimate — our first size signal, before cloning
    archived: bool = False
    is_fork: bool = False
    license_key: str | None = None
    topics: tuple[str, ...] = ()
    pushed_at: datetime | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Self:
        return cls(
            full_name=data["full_name"],
            owner=data["owner"]["login"],
            name=data["name"],
            default_branch=data.get("default_branch", "main"),
            clone_url=data["clone_url"],
            html_url=data["html_url"],
            description=data.get("description"),
            primary_language=data.get("language"),
            stars=data.get("stargazers_count", 0),
            open_issues_count=data.get("open_issues_count", 0),
            size_kb=data.get("size", 0),
            archived=data.get("archived", False),
            is_fork=data.get("fork", False),
            license_key=(data.get("license") or {}).get("key"),
            topics=tuple(data.get("topics") or ()),
            pushed_at=data.get("pushed_at"),
        )


class Issue(Frozen):
    """A GitHub issue.

    Note ``is_pull_request``. GitHub's issues endpoint returns pull requests
    too — every PR is an issue underneath. Forgetting this is the single most
    common bug when first using this API, and it quietly poisons any issue
    ranking with entries that are already solved.
    """

    number: int
    title: str
    body: str | None
    state: IssueState
    labels: tuple[Label, ...] = ()
    assignees: tuple[str, ...] = ()
    author: str | None = None
    comments_count: int = 0
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    html_url: str
    is_pull_request: bool = False

    @property
    def label_names(self) -> frozenset[str]:
        return frozenset(label.name.lower() for label in self.labels)

    @property
    def is_assigned(self) -> bool:
        return bool(self.assignees)

    @property
    def text(self) -> str:
        """Title and body as one blob — what we embed and search over."""
        return f"{self.title}\n\n{self.body or ''}".strip()

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Self:
        return cls(
            number=data["number"],
            title=data["title"],
            body=data.get("body"),
            state=IssueState(data["state"]),
            labels=tuple(
                Label(name=lbl["name"], description=lbl.get("description"))
                for lbl in data.get("labels", [])
            ),
            assignees=tuple(a["login"] for a in data.get("assignees", [])),
            author=(data.get("user") or {}).get("login"),
            comments_count=data.get("comments", 0),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            closed_at=data.get("closed_at"),
            html_url=data["html_url"],
            is_pull_request="pull_request" in data,
        )


class RateLimitStatus(Frozen):
    """What GitHub told us about our remaining request budget.

    Read from response headers on every call, so we always know where we stand
    without spending a request to ask.
    """

    limit: int
    remaining: int
    reset_at: datetime
    resource: str = "core"

    @property
    def is_exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def fraction_used(self) -> float:
        return 0.0 if self.limit == 0 else 1.0 - (self.remaining / self.limit)


# --- Local repository analysis ----------------------------------------------


class PackageManager(StrEnum):
    POETRY = "poetry"
    PIP = "pip"
    PDM = "pdm"
    HATCH = "hatch"
    UV = "uv"
    SETUPTOOLS = "setuptools"
    UNKNOWN = "unknown"


class TestRunner(StrEnum):
    """The test runner a repository uses.

    ``__test__ = False`` because pytest tries to collect any class whose name
    begins with ``Test`` and then warns that it cannot instantiate it. This is
    the documented opt-out. It is safe on an Enum specifically: names with
    leading *and* trailing double underscores are not turned into enum members,
    so this does not create a ``TestRunner.__test__`` value.
    """

    __test__ = False

    PYTEST = "pytest"
    UNITTEST = "unittest"
    NOSE = "nose"
    UNKNOWN = "unknown"


class Commit(Frozen):
    """One commit, as read from a local clone."""

    sha: str
    author: str
    authored_at: datetime
    subject: str
    files_changed: tuple[str, ...] = ()

    @property
    def short_sha(self) -> str:
        return self.sha[:8]


class RepositorySnapshot(Frozen):
    """Everything we know about a repository at one moment.

    This is the contract between Phase 1 and everything after it. The sandbox
    runs ``test_command``. RAG indexes ``source_files``. The benchmark pins
    ``head_sha`` so results are reproducible. If a field here is wrong, the
    error surfaces three phases later wearing a disguise — which is exactly why
    this phase is deterministic and heavily tested.
    """

    repository: Repository
    local_path: Path
    head_sha: str

    languages: tuple[str, ...] = ()
    package_manager: PackageManager = PackageManager.UNKNOWN
    test_runner: TestRunner = TestRunner.UNKNOWN

    test_command: tuple[str, ...] | None = None
    lint_command: tuple[str, ...] | None = None
    typecheck_command: tuple[str, ...] | None = None

    source_files: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()
    doc_files: tuple[str, ...] = ()
    config_files: tuple[str, ...] = ()

    python_requires: str | None = None
    has_ci: bool = False
    total_files: int = 0
    total_bytes: int = 0

    analyzed_at: datetime = Field(default_factory=datetime.now)

    @property
    def is_testable(self) -> bool:
        """Can we validate a patch here at all?

        A repository with no discoverable test command cannot be worked on
        safely — we would have no way to tell a fix from a break.
        """
        return self.test_command is not None and bool(self.test_files)

    def summary(self) -> str:
        """One-paragraph description, for prompts and CLI output."""
        return (
            f"{self.repository.full_name} @ {self.head_sha[:8]} — "
            f"{self.total_files} files, languages={', '.join(self.languages) or 'unknown'}, "
            f"package manager={self.package_manager}, tests={self.test_runner} "
            f"({len(self.test_files)} test files), "
            f"test command={' '.join(self.test_command) if self.test_command else 'NOT FOUND'}"
        )
