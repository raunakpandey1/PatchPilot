"""GitHub API client.

Issues and pull requests do **not** live in the git repository. They live in
GitHub's database, reachable only over HTTP. So PatchPilot talks to two entirely
separate systems about the same project: git for code and history (see
``tools/git.py``), and this module for everything else.

This client is deliberately not a thin wrapper around ``httpx``. Four concerns
are baked in, because each of them is a way the naive version fails in
production:

1. **Rate limits.** 5,000 requests/hour authenticated, 60 unauthenticated. We
   read the remaining budget from every response so we always know where we
   stand, and refuse to start work we cannot finish.
2. **Conditional requests.** Unchanged data returns ``304`` and does not count
   against the budget. See ``http_cache.py``.
3. **Pagination.** GitHub returns at most 100 items per page and puts the next
   page's URL in a ``Link`` header. Guessing page numbers is how you silently
   miss data.
4. **Retries.** Networks fail and GitHub occasionally returns 5xx. Retrying
   *some* failures (timeouts, 502) and never retrying others (404, 401) is the
   difference between resilience and hammering a dead endpoint.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from patchpilot.errors import (
    AuthenticationError,
    GitHubError,
    IssueNotFound,
    RateLimitExceeded,
    RepositoryNotFound,
)
from patchpilot.logging import get_logger
from patchpilot.models import Issue, RateLimitStatus, Repository
from patchpilot.tools.http_cache import NullCache, ResponseCache

log = get_logger("github")

# GitHub's documented maximum. Asking for more is ignored; asking for less
# means more requests for the same data.
MAX_PER_PAGE = 100

# A runaway pagination loop would drain the whole budget. This is a circuit
# breaker, not a real expectation.
MAX_PAGES = 50


class TransientHTTPError(GitHubError):
    """A failure that is worth retrying: 5xx, or a secondary rate limit."""


class GitHubClient:
    """Read-only GitHub API client.

    Read-only is enforced by what this class does not implement: there is no
    method here that writes. Phase 9 adds writes, behind human approval, in a
    separate class — so "can the agent open a PR without asking?" is answerable
    by reading the type, not by auditing prompts.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        api_url: str = "https://api.github.com",
        timeout_s: float = 30.0,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
        max_attempts: int = 4,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 30.0,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        # `cache if cache is not None` — NOT `cache or NullCache()`.
        # MemoryCache defines __len__, so an empty cache is falsy and `or`
        # would silently discard it and install a no-op cache instead. The
        # symptom is subtle: everything works, just at 5,000 requests/hour
        # instead of effectively unlimited.
        self.cache: ResponseCache = cache if cache is not None else NullCache()
        self._authenticated = token is not None

        headers = {
            "Accept": "application/vnd.github+json",
            # Pinning the API version means GitHub cannot change response
            # shapes underneath us without us opting in.
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "patchpilot",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        # `transport` is injectable purely so tests can supply recorded
        # responses. The client has no idea whether it is talking to the real
        # GitHub — which is the point.
        self._client = httpx.Client(
            base_url=self.api_url,
            headers=headers,
            timeout=timeout_s,
            transport=transport,
            follow_redirects=True,
        )

        self._rate_limit: RateLimitStatus | None = None
        self._requests_made = 0
        self._cache_hits = 0

        # Retry policy built here rather than as a decorator so tests can
        # collapse the backoff to zero. A decorator would bake the real waits
        # into the class and make the retry path untestable in under a minute.
        self._retrying = Retrying(
            retry=retry_if_exception_type((httpx.TransportError, TransientHTTPError)),
            wait=wait_exponential_jitter(initial=backoff_initial_s, max=backoff_max_s),
            stop=stop_after_attempt(max_attempts),
            reraise=True,
        )

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- introspection -----------------------------------------------------

    @property
    def rate_limit(self) -> RateLimitStatus | None:
        """Budget as of the last response. ``None`` before the first request."""
        return self._rate_limit

    @property
    def stats(self) -> dict[str, int]:
        """Counters for the metrics report.

        ``cache_hits`` is the number of 304s — requests that cost no budget.
        This is the number that proves conditional requests are working.
        """
        return {
            "requests_made": self._requests_made,
            "cache_hits": self._cache_hits,
            "authenticated": int(self._authenticated),
        }

    # --- transport ---------------------------------------------------------

    def _send(self, request: httpx.Request) -> httpx.Response:
        """Send one request, retrying only failures that retrying can fix.

        Exponential backoff with jitter: doubling the wait gives a struggling
        server room to recover, and the random jitter stops many clients from
        retrying in lockstep and re-creating the overload (a thundering herd).

        Note what is *not* retried: 404, 401, and an exhausted primary rate
        limit. Retrying those cannot change the answer, and doing so turns a
        clear error into a slow one.
        """
        return self._retrying(self._send_once, request)

    def _send_once(self, request: httpx.Request) -> httpx.Response:
        # httpx requests are single-use once sent through some transports, so
        # rebuild before each attempt.
        response = self._client.send(self._client.build_request(
            request.method, request.url, headers=request.headers, content=request.content,
        ))
        self._record_rate_limit(response)

        if response.status_code >= 500:
            raise TransientHTTPError(f"GitHub returned {response.status_code}")

        # A 403 or 429 means one of two different things, and they need
        # opposite responses.
        if response.status_code in (403, 429):
            remaining = response.headers.get("x-ratelimit-remaining")
            if remaining == "0":
                # Primary budget gone. Retrying cannot help — the caller must
                # decide whether to wait until reset.
                raise RateLimitExceeded(
                    reset_at=self._parse_reset(response),
                    limit=_int_or_none(response.headers.get("x-ratelimit-limit")),
                )
            # Secondary limit: too many requests too fast. This one *is*
            # worth retrying after a pause.
            raise TransientHTTPError(
                f"Secondary rate limit ({response.status_code}); "
                f"retry-after={response.headers.get('retry-after')}"
            )

        return response

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET one URL and return the decoded JSON body."""
        payload, _ = self._request_with_headers(path, params)
        return payload

    def _request_with_headers(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[Any, httpx.Headers]:
        """GET one URL, using the conditional-request cache.

        Returns the decoded JSON body — from GitHub on a 200, or from the cache
        on a 304 — together with the response headers. Pagination needs those
        headers, and passing them back explicitly is safer than leaving them on
        ``self`` where a concurrent call could overwrite them.
        """
        url = self._client.build_request("GET", path, params=params).url
        cache_key = str(url)
        cached = self.cache.get(cache_key)

        headers = {"If-None-Match": cached.etag} if cached else {}
        request = self._client.build_request("GET", path, params=params, headers=headers)

        self._requests_made += 1
        response = self._send(request)

        if response.status_code == 304 and cached is not None:
            self._cache_hits += 1
            log.debug("github_not_modified", url=cache_key)
            # GitHub does not send Link on a 304, so restore the one we stored
            # alongside this payload. Without this, pagination stops after the
            # first page whenever the cache is warm.
            response_headers = httpx.Headers(response.headers)
            if cached.link:
                response_headers["link"] = cached.link
            return cached.payload, response_headers

        self._raise_for_status(response, path)

        payload = response.json()
        if etag := response.headers.get("etag"):
            self.cache.set(cache_key, etag, payload, response.headers.get("link"))
        return payload, response.headers

    def _paginate(self, path: str, params: dict[str, Any] | None = None) -> Iterator[Any]:
        """Yield items across every page.

        Follows the ``Link: <...>; rel="next"`` header rather than incrementing
        a page counter. GitHub is the authority on whether another page exists;
        guessing produces silent truncation or an extra wasted request.
        """
        params = dict(params or {})
        params.setdefault("per_page", MAX_PER_PAGE)

        next_path: str | None = path
        next_params: dict[str, Any] | None = params

        for page in range(MAX_PAGES):
            if next_path is None:
                return
            payload, headers = self._request_with_headers(next_path, next_params)
            if not isinstance(payload, list):
                raise GitHubError(f"Expected a list from {next_path}, got {type(payload).__name__}")
            yield from payload

            # After the first request the `next` URL already carries the query
            # string, so params must not be re-applied.
            next_path = self._next_link(headers)
            next_params = None

            if next_path is None:
                return
            log.debug("github_paginating", page=page + 2, url=next_path)

        log.warning("github_pagination_cap_hit", path=path, max_pages=MAX_PAGES)

    # --- public API --------------------------------------------------------

    def get_repository(self, full_name: str) -> Repository:
        """Fetch repository metadata. ``full_name`` is ``"owner/name"``."""
        data = self._request(f"/repos/{full_name}")
        return Repository.from_api(data)

    def get_issue(self, full_name: str, number: int) -> Issue:
        try:
            data = self._request(f"/repos/{full_name}/issues/{number}")
        except RepositoryNotFound:
            raise IssueNotFound(full_name, number) from None
        return Issue.from_api(data)

    def list_issues(
        self,
        full_name: str,
        *,
        state: str = "open",
        labels: list[str] | None = None,
        since: datetime | None = None,
        include_pull_requests: bool = False,
        limit: int | None = None,
    ) -> list[Issue]:
        """List issues, newest activity first.

        ``since`` is the incremental-sync lever: GitHub returns only issues
        updated after that timestamp, so a repeat sync costs a fraction of a
        full one.

        ``include_pull_requests`` defaults to False because GitHub's issues
        endpoint returns pull requests as well. Leaving them in means ranking
        already-solved work as though it were available.
        """
        params: dict[str, Any] = {"state": state, "sort": "updated", "direction": "desc"}
        if labels:
            params["labels"] = ",".join(labels)
        if since:
            params["since"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        issues: list[Issue] = []
        for raw in self._paginate(f"/repos/{full_name}/issues", params):
            issue = Issue.from_api(raw)
            if issue.is_pull_request and not include_pull_requests:
                continue
            issues.append(issue)
            if limit is not None and len(issues) >= limit:
                break

        log.info(
            "issues_listed",
            repo=full_name,
            count=len(issues),
            state=state,
            incremental=since is not None,
            **self.stats,
        )
        return issues

    def get_issue_comments(self, full_name: str, number: int) -> list[dict[str, Any]]:
        """Comments on an issue — often where the real reproduction lives."""
        return list(self._paginate(f"/repos/{full_name}/issues/{number}/comments"))

    # --- internals ---------------------------------------------------------

    def _record_rate_limit(self, response: httpx.Response) -> None:
        remaining = _int_or_none(response.headers.get("x-ratelimit-remaining"))
        limit = _int_or_none(response.headers.get("x-ratelimit-limit"))
        reset_at = self._parse_reset(response)
        if remaining is None or limit is None or reset_at is None:
            return
        self._rate_limit = RateLimitStatus(
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
            resource=response.headers.get("x-ratelimit-resource", "core"),
        )
        if self._rate_limit.fraction_used > 0.9:
            log.warning(
                "github_rate_limit_low",
                remaining=remaining,
                limit=limit,
                reset_at=reset_at.isoformat(),
            )

    @staticmethod
    def _parse_reset(response: httpx.Response) -> datetime | None:
        raw = response.headers.get("x-ratelimit-reset")
        if not raw:
            return None
        try:
            return datetime.fromtimestamp(int(raw), tz=UTC)
        except (ValueError, OSError):
            return None

    @staticmethod
    def _next_link(headers: httpx.Headers) -> str | None:
        """Extract the ``rel="next"`` URL from a Link header, if present."""
        link = headers.get("link")
        if not link:
            return None
        for part in link.split(","):
            section = part.split(";")
            if len(section) < 2:
                continue
            if 'rel="next"' in section[1].strip():
                url: str = section[0].strip().strip("<>")
                return url
        return None

    @staticmethod
    def _raise_for_status(response: httpx.Response, path: str) -> None:
        if response.status_code == 404:
            raise RepositoryNotFound(path.strip("/").removeprefix("repos/"))
        if response.status_code == 401:
            raise AuthenticationError(
                "GitHub rejected the token. Check PATCHPILOT_GITHUB_TOKEN is set "
                "and has not expired."
            )
        if response.status_code >= 400:
            raise GitHubError(f"GitHub returned {response.status_code} for {path}: {response.text[:300]}")


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
