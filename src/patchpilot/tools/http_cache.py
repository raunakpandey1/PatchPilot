"""Conditional-request cache for the GitHub API.

The problem this solves
-----------------------
An authenticated GitHub token gets 5,000 requests per hour. Listing the issues
of one busy repository can cost dozens of them. Poll a few repositories on a
loop and the budget is gone before lunch.

But almost nothing changes between polls. GitHub gives every response an
``ETag`` — a short fingerprint of its content. Send that fingerprint back on the
next request as ``If-None-Match`` and, if the data has not changed, GitHub
replies ``304 Not Modified`` with an empty body. That reply is documented as
**not counting against your rate limit**.

So the cache is not a latency optimisation. It is what makes the budget last.

The design
----------
``ResponseCache`` is a Protocol — a shape, not a base class. The GitHub client
depends on the shape, so tests can pass a dictionary-backed cache and production
can pass a disk-backed one, with no flag and no branch in the client.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from patchpilot.logging import get_logger

log = get_logger("http_cache")


class CachedResponse:
    """An ETag, the body it belongs to, and the pagination link that came with it.

    ``link`` is not an optimisation. GitHub omits the ``Link`` header from 304
    responses, so a cached page has no way to say "there is a next page" unless
    we stored that header when we first saw it. Without this field, a warm
    cache silently truncates every paginated result to its first page.
    """

    __slots__ = ("etag", "link", "payload")

    def __init__(self, etag: str, payload: Any, link: str | None = None) -> None:
        self.etag = etag
        self.payload = payload
        self.link = link


class ResponseCache(Protocol):
    """What the GitHub client needs from a cache. Nothing more."""

    def get(self, key: str) -> CachedResponse | None: ...

    def set(self, key: str, etag: str, payload: Any, link: str | None = None) -> None: ...


class NullCache:
    """Caches nothing. Useful for isolating whether a bug is a caching bug."""

    def get(self, key: str) -> CachedResponse | None:
        return None

    def set(self, key: str, etag: str, payload: Any, link: str | None = None) -> None:
        return None


class MemoryCache:
    """In-process cache. Used by tests, and for a single short-lived run."""

    def __init__(self) -> None:
        self._entries: dict[str, CachedResponse] = {}

    def get(self, key: str) -> CachedResponse | None:
        return self._entries.get(key)

    def set(self, key: str, etag: str, payload: Any, link: str | None = None) -> None:
        self._entries[key] = CachedResponse(etag, payload, link)

    def __len__(self) -> int:
        return len(self._entries)


class FileCache:
    """Disk cache — one JSON file per request URL.

    One file per entry rather than one big file: concurrent writers cannot
    corrupt each other's entries, and a single malformed file loses one cached
    response instead of the whole cache.

    The filename is a hash of the key because URLs contain ``/`` and ``?`` and
    can exceed filesystem name limits.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def get(self, key: str) -> CachedResponse | None:
        path = self._path_for(key)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            return CachedResponse(raw["etag"], raw["payload"], raw.get("link"))
        except (json.JSONDecodeError, KeyError, OSError):
            # A corrupt cache entry must never break the caller. Treat it as a
            # miss; the next successful response overwrites it.
            log.warning("cache_entry_unreadable", path=str(path))
            return None

    def set(self, key: str, etag: str, payload: Any, link: str | None = None) -> None:
        path = self._path_for(key)
        # Write to a temp file then rename: rename is atomic on POSIX, so a
        # crash mid-write cannot leave a half-written entry behind.
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps({"key": key, "etag": etag, "payload": payload, "link": link})
            )
            tmp.replace(path)
        except OSError:
            log.warning("cache_write_failed", path=str(path))
