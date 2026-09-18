"""GitHub client tests — no network.

Every test here uses ``httpx.MockTransport``: a fake transport that returns
whatever we tell it to. The client is handed that transport in its constructor
and cannot tell the difference, which is the entire reason the constructor
accepts one.

This is the answer to "how do you test code that depends on a third-party API?"
Recorded, deterministic responses. Tests run offline, in milliseconds, with no
rate limit and no token — so they actually get run.
"""

from __future__ import annotations

import time

import httpx
import pytest

from patchpilot.errors import (
    AuthenticationError,
    GitHubError,
    RateLimitExceeded,
    RepositoryNotFound,
)
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.http_cache import MemoryCache

REPO_JSON = {
    "full_name": "simonw/sqlite-utils",
    "owner": {"login": "simonw"},
    "name": "sqlite-utils",
    "default_branch": "main",
    "clone_url": "https://github.com/simonw/sqlite-utils.git",
    "html_url": "https://github.com/simonw/sqlite-utils",
    "language": "Python",
    "stargazers_count": 1700,
    "size": 4200,
}


def issue_json(number: int, *, is_pr: bool = False) -> dict:
    data = {
        "number": number,
        "title": f"Issue {number}",
        "body": "body",
        "state": "open",
        "labels": [],
        "assignees": [],
        "user": {"login": "someone"},
        "comments": 0,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "html_url": f"https://github.com/o/r/issues/{number}",
    }
    if is_pr:
        data["pull_request"] = {"url": "..."}
    return data


def rate_limit_headers(remaining: int = 4999, limit: int = 5000) -> dict[str, str]:
    return {
        "x-ratelimit-limit": str(limit),
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int(time.time()) + 3600),
        "x-ratelimit-resource": "core",
    }


def client_for(handler, **kwargs) -> GitHubClient:
    kwargs.setdefault("max_attempts", 2)
    kwargs.setdefault("backoff_initial_s", 0.0)
    kwargs.setdefault("backoff_max_s", 0.0)
    return GitHubClient(token="fake", transport=httpx.MockTransport(handler), **kwargs)


# --- happy path -------------------------------------------------------------


def test_get_repository_parses_response():
    with client_for(lambda r: httpx.Response(200, json=REPO_JSON, headers=rate_limit_headers())) as gh:
        repo = gh.get_repository("simonw/sqlite-utils")

    assert repo.full_name == "simonw/sqlite-utils"
    assert repo.primary_language == "Python"


def test_rate_limit_is_read_from_every_response():
    """We always know the remaining budget without spending a request to ask."""
    with client_for(lambda r: httpx.Response(200, json=REPO_JSON, headers=rate_limit_headers(4321))) as gh:
        gh.get_repository("o/r")
        assert gh.rate_limit is not None
        assert gh.rate_limit.remaining == 4321
        assert gh.rate_limit.is_exhausted is False


# --- conditional requests ---------------------------------------------------


def test_etag_is_sent_on_the_second_request_and_304_serves_cache():
    """The behaviour that makes the request budget last.

    First call: 200 with an ETag, which we store. Second call: we send
    ``If-None-Match``, GitHub answers 304 with an empty body, and we serve the
    cached payload. A 304 does not count against the rate limit.
    """
    seen_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == '"abc123"':
            return httpx.Response(304, headers=rate_limit_headers())
        return httpx.Response(
            200, json=REPO_JSON, headers={**rate_limit_headers(), "etag": '"abc123"'}
        )

    cache = MemoryCache()
    with client_for(handler, cache=cache) as gh:
        first = gh.get_repository("o/r")
        second = gh.get_repository("o/r")

    assert seen_headers == [None, '"abc123"']
    assert first == second
    assert gh.stats["cache_hits"] == 1
    assert len(cache) == 1


def test_cache_miss_when_content_changed():
    """A changed ETag must serve fresh data, not the stale cached copy."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(
                200,
                json={**REPO_JSON, "stargazers_count": 9999},
                headers={**rate_limit_headers(), "etag": '"v2"'},
            )
        return httpx.Response(200, json=REPO_JSON, headers={**rate_limit_headers(), "etag": '"v1"'})

    with client_for(handler, cache=MemoryCache()) as gh:
        assert gh.get_repository("o/r").stars == 1700
        assert gh.get_repository("o/r").stars == 9999
        assert gh.stats["cache_hits"] == 0


# --- pagination -------------------------------------------------------------


def test_pagination_follows_link_header():
    """GitHub decides whether another page exists; we never guess page numbers."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "page=2" in str(request.url):
            return httpx.Response(200, json=[issue_json(3)], headers=rate_limit_headers())
        return httpx.Response(
            200,
            json=[issue_json(1), issue_json(2)],
            headers={
                **rate_limit_headers(),
                "link": '<https://api.github.com/repos/o/r/issues?page=2>; rel="next", '
                        '<https://api.github.com/repos/o/r/issues?page=2>; rel="last"',
            },
        )

    with client_for(handler) as gh:
        issues = gh.list_issues("o/r")

    assert [i.number for i in issues] == [1, 2, 3]


def test_pull_requests_are_excluded_by_default():
    """The issues endpoint returns PRs too. Ranking them would be nonsense."""
    payload = [issue_json(1), issue_json(2, is_pr=True), issue_json(3)]

    with client_for(lambda r: httpx.Response(200, json=payload, headers=rate_limit_headers())) as gh:
        assert [i.number for i in gh.list_issues("o/r")] == [1, 3]
        assert [i.number for i in gh.list_issues("o/r", include_pull_requests=True)] == [1, 2, 3]


def test_limit_stops_early():
    payload = [issue_json(n) for n in range(1, 11)]
    with client_for(lambda r: httpx.Response(200, json=payload, headers=rate_limit_headers())) as gh:
        assert len(gh.list_issues("o/r", limit=3)) == 3


# --- failures ---------------------------------------------------------------


def test_404_raises_repository_not_found():
    with client_for(lambda r: httpx.Response(404, json={"message": "Not Found"})) as gh:
        with pytest.raises(RepositoryNotFound):
            gh.get_repository("nope/nope")


def test_401_raises_authentication_error():
    with client_for(lambda r: httpx.Response(401, json={"message": "Bad credentials"})) as gh:
        with pytest.raises(AuthenticationError):
            gh.get_repository("o/r")


def test_exhausted_primary_rate_limit_raises_immediately():
    """Not retried: the budget is gone, and retrying cannot bring it back.

    The error carries ``reset_at`` so the caller can decide whether to wait.
    """
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, headers=rate_limit_headers(remaining=0), json={"message": "rate limited"})

    with client_for(handler) as gh:
        with pytest.raises(RateLimitExceeded) as exc:
            gh.get_repository("o/r")

    assert attempts == 1, "an exhausted rate limit must not be retried"
    assert exc.value.reset_at is not None


def test_secondary_rate_limit_is_retried():
    """Different from the primary limit: this one means 'too fast', so back off."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                403, headers={**rate_limit_headers(remaining=100), "retry-after": "1"},
                json={"message": "secondary rate limit"},
            )
        return httpx.Response(200, json=REPO_JSON, headers=rate_limit_headers())

    with client_for(handler) as gh:
        assert gh.get_repository("o/r").full_name == "simonw/sqlite-utils"
    assert attempts == 2


def test_server_errors_are_retried_then_surface():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="unavailable")

    with client_for(handler, max_attempts=3) as gh:
        with pytest.raises(GitHubError):
            gh.get_repository("o/r")

    assert attempts == 3, "5xx should be retried up to the attempt limit"


def test_transient_error_recovers_without_the_caller_knowing():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectTimeout("network blip")
        return httpx.Response(200, json=REPO_JSON, headers=rate_limit_headers())

    with client_for(handler, max_attempts=4) as gh:
        assert gh.get_repository("o/r").name == "sqlite-utils"
    assert attempts == 3


def test_pagination_survives_a_warm_cache():
    """Regression test for a bug found by measurement, not by testing.

    GitHub omits the ``Link`` header from 304 responses. The first version of
    this client read the next-page URL from the live response, so as soon as
    the cache was warm the link vanished and pagination stopped after page one.

    The symptom was quiet and easy to miss: a cold run returned 77 issues, a
    warm run returned 61. Nothing errored. The fix is to store the Link header
    with the cached payload and restore it on a 304.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append(url)
        is_page_two = "page=2" in url
        etag = '"page2"' if is_page_two else '"page1"'

        if request.headers.get("if-none-match") == etag:
            # Note: no Link header, exactly like the real API.
            return httpx.Response(304, headers={**rate_limit_headers(), "etag": etag})

        if is_page_two:
            return httpx.Response(
                200, json=[issue_json(3)], headers={**rate_limit_headers(), "etag": etag}
            )
        return httpx.Response(
            200,
            json=[issue_json(1), issue_json(2)],
            headers={
                **rate_limit_headers(),
                "etag": etag,
                "link": '<https://api.github.com/repos/o/r/issues?page=2>; rel="next"',
            },
        )

    cache = MemoryCache()

    with client_for(handler, cache=cache) as gh:
        cold = gh.list_issues("o/r")
    with client_for(handler, cache=cache) as gh:
        warm = gh.list_issues("o/r")

    assert [i.number for i in cold] == [1, 2, 3]
    assert [i.number for i in warm] == [1, 2, 3], "a warm cache must not truncate pagination"
    assert gh.stats["cache_hits"] == 2, "both pages should have been served from cache"
