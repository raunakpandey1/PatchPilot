"""Measure what conditional requests (ETags) actually save.

The claim: a 304 "Not Modified" response does not count against the GitHub rate
limit. This script tests that claim by reading the remaining budget before and
after an identical second fetch.

    poetry run python scripts/benchmark_github.py [owner/name]
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

from patchpilot.tools.github import GitHubClient
from patchpilot.tools.http_cache import FileCache


def token() -> str | None:
    """Borrow the gh CLI's token rather than asking for one to be stored."""
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run(label: str, gh: GitHubClient, full_name: str) -> dict:
    before = gh.rate_limit.remaining if gh.rate_limit else None
    started = time.monotonic()
    issues = gh.list_issues(full_name, limit=200)
    elapsed = time.monotonic() - started
    after = gh.rate_limit.remaining if gh.rate_limit else None

    return {
        "label": label,
        "issues": len(issues),
        "seconds": round(elapsed, 2),
        "http_requests": gh.stats["requests_made"],
        "cache_hits_304": gh.stats["cache_hits"],
        "budget_spent": (before - after) if (before is not None and after is not None) else None,
    }


def main() -> None:
    full_name = sys.argv[1] if len(sys.argv) > 1 else "simonw/sqlite-utils"

    with tempfile.TemporaryDirectory() as tmp:
        cache = FileCache(Path(tmp) / "cache")

        # Cold: nothing cached, every page is a real 200.
        with GitHubClient(token=token(), cache=cache) as gh:
            gh.get_repository(full_name)  # prime rate-limit reading
            cold = run("cold cache", gh, full_name)

        # Warm: same data, ETags sent, GitHub answers 304.
        with GitHubClient(token=token(), cache=cache) as gh:
            gh.get_repository(full_name)
            warm = run("warm cache", gh, full_name)

    header = f"{'run':<12} {'issues':>7} {'seconds':>8} {'requests':>9} {'304s':>6} {'budget spent':>13}"
    print(f"\n{full_name}\n{header}\n{'-' * len(header)}")
    for row in (cold, warm):
        print(
            f"{row['label']:<12} {row['issues']:>7} {row['seconds']:>8.2f} "
            f"{row['http_requests']:>9} {row['cache_hits_304']:>6} {row['budget_spent']!s:>13}"
        )


if __name__ == "__main__":
    main()
