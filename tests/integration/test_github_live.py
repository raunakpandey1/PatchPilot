"""Tests that talk to the real GitHub and clone a real repository.

These are marked ``integration`` and excluded from the default run:

    poetry run pytest -m "not integration"   # the fast suite, run constantly
    poetry run pytest -m integration          # run deliberately

Why separate them at all: these are slow, need the network, consume rate limit,
and fail when GitHub has a bad day. A suite with those properties stops being
run, and a suite that is not run is worth nothing. The unit suite stays fast so
it gets run on every change; these exist to catch the thing unit tests
structurally cannot — that our idea of the API matches the real one.
"""

from __future__ import annotations

import subprocess

import pytest

from patchpilot.analysis.repository import analyze_repository
from patchpilot.models import PackageManager, TestRunner
from patchpilot.tools.git import CloneStrategy, clone
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.http_cache import MemoryCache

pytestmark = pytest.mark.integration

TARGET = "simonw/sqlite-utils"


def github_token() -> str | None:
    """Borrow the gh CLI's token so no secret has to be stored for tests."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


@pytest.fixture(scope="module")
def client():
    with GitHubClient(token=github_token(), cache=MemoryCache()) as gh:
        yield gh


def test_fetches_the_real_repository(client):
    repo = client.get_repository(TARGET)

    assert repo.full_name == TARGET
    assert repo.primary_language == "Python"
    assert repo.default_branch


def test_lists_real_open_issues_without_pull_requests(client):
    issues = client.list_issues(TARGET, limit=30)

    assert issues, "the target repository should have open issues"
    assert all(not i.is_pull_request for i in issues)
    assert all(i.state == "open" for i in issues)


def test_conditional_requests_cost_no_rate_limit_budget(client):
    """The claim that makes the free tier viable, checked against the real API."""
    client.list_issues(TARGET, limit=30)
    before = client.rate_limit.remaining
    hits_before = client.stats["cache_hits"]

    client.list_issues(TARGET, limit=30)

    assert client.rate_limit.remaining == before, "a 304 must not spend budget"
    assert client.stats["cache_hits"] > hits_before


def test_warm_cache_returns_the_same_issues_as_a_cold_one(client):
    """Regression guard for the Link-header bug (see docs/failures.md, F-002)."""
    cold = client.list_issues(TARGET, limit=200)
    warm = client.list_issues(TARGET, limit=200)

    assert [i.number for i in cold] == [i.number for i in warm]


def test_missing_repository_raises_not_found(client):
    from patchpilot.errors import RepositoryNotFound

    with pytest.raises(RepositoryNotFound):
        client.get_repository("patchpilot-test/definitely-does-not-exist-12345")


def test_clone_and_analyze_the_real_target(client, tmp_path):
    """End-to-end Phase 1: GitHub metadata → clone → snapshot."""
    repo = client.get_repository(TARGET)

    local = clone(repo.clone_url, tmp_path / "clone", strategy=CloneStrategy.BLOBLESS)
    snapshot = analyze_repository(repo, local)

    assert snapshot.head_sha
    assert "Python" in snapshot.languages
    assert snapshot.test_runner is TestRunner.PYTEST
    assert snapshot.package_manager is not PackageManager.UNKNOWN
    assert snapshot.is_testable, "the benchmark target must be testable"
    assert snapshot.source_files and snapshot.test_files

    # Blobless keeps history — the capability a shallow clone would cost us.
    assert len(local.log(max_count=50)) == 50
    assert local.files_changed_in(local.log(max_count=1)[0].sha)
