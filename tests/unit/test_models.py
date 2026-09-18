"""Model tests — especially the parsing rules that are easy to get wrong."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from patchpilot.models import Issue, IssueState, PackageManager, Repository, TestRunner

ISSUE_JSON = {
    "number": 42,
    "title": "Crash when table name contains a dot",
    "body": "Steps to reproduce...",
    "state": "open",
    "labels": [{"name": "bug", "description": "Something is broken"}],
    "assignees": [],
    "user": {"login": "someone"},
    "comments": 3,
    "created_at": "2026-01-01T10:00:00Z",
    "updated_at": "2026-01-02T10:00:00Z",
    "html_url": "https://github.com/o/r/issues/42",
}


def test_issue_parses_core_fields():
    issue = Issue.from_api(ISSUE_JSON)

    assert issue.number == 42
    assert issue.state is IssueState.OPEN
    assert issue.label_names == {"bug"}
    assert issue.is_assigned is False
    assert issue.author == "someone"


def test_pull_requests_are_flagged():
    """GitHub's issues endpoint returns PRs too, marked by a `pull_request` key.

    Missing this means ranking already-solved work as available work — the most
    common first bug when using this API.
    """
    assert Issue.from_api(ISSUE_JSON).is_pull_request is False

    as_pr = {**ISSUE_JSON, "pull_request": {"url": "https://api.github.com/..."}}
    assert Issue.from_api(as_pr).is_pull_request is True


def test_issue_text_combines_title_and_body():
    issue = Issue.from_api(ISSUE_JSON)
    assert issue.title in issue.text
    assert "Steps to reproduce" in issue.text


def test_issue_with_null_body_does_not_crash():
    """An issue with an empty body is common and must not break embedding."""
    issue = Issue.from_api({**ISSUE_JSON, "body": None})
    assert issue.text == issue.title


def test_models_are_immutable():
    """Nodes share references to these objects; mutation would be a silent bug."""
    issue = Issue.from_api(ISSUE_JSON)
    with pytest.raises(ValidationError):
        issue.title = "changed"


def test_unknown_api_fields_are_rejected_loudly():
    """`extra="forbid"` means a GitHub schema change surfaces here, not later."""
    with pytest.raises(ValidationError):
        Issue(**{**Issue.from_api(ISSUE_JSON).model_dump(), "surprise": 1})


def test_repository_parses_and_tolerates_missing_optionals():
    repo = Repository.from_api(
        {
            "full_name": "simonw/sqlite-utils",
            "owner": {"login": "simonw"},
            "name": "sqlite-utils",
            "clone_url": "https://github.com/simonw/sqlite-utils.git",
            "html_url": "https://github.com/simonw/sqlite-utils",
        }
    )
    assert repo.full_name == "simonw/sqlite-utils"
    assert repo.default_branch == "main"
    assert repo.primary_language is None
    assert repo.topics == ()


@pytest.mark.parametrize(
    ("member", "value"),
    [(PackageManager.POETRY, "poetry"), (TestRunner.PYTEST, "pytest")],
)
def test_enums_serialise_as_plain_strings(member, value):
    """StrEnum keeps checkpoint files readable and JSON round-trips simple."""
    assert member == value
    assert f"{member}" == value
