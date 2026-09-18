"""Measure the three clone strategies on a real repository.

Produces the numbers quoted in docs/metrics.md. Run:

    poetry run python scripts/benchmark_clone.py [owner/name]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

from patchpilot.tools.git import CloneStrategy, clone


def measure(url: str, strategy: CloneStrategy, root: Path) -> dict:
    dest = root / strategy.value
    started = time.monotonic()
    repo = clone(url, dest, strategy=strategy, timeout_s=600, max_size_mb=2000)
    elapsed = time.monotonic() - started

    # Can we still read history? This is the capability shallow clones lose.
    try:
        commits = len(repo.log(max_count=200))
    except Exception:
        commits = 0

    result = {
        "strategy": strategy.value,
        "seconds": round(elapsed, 2),
        "size_mb": round(repo.size_mb or 0, 1),
        "files": len(repo.list_files()),
        "commits_readable": commits,
    }
    shutil.rmtree(dest, ignore_errors=True)
    return result


def main() -> None:
    full_name = sys.argv[1] if len(sys.argv) > 1 else "simonw/sqlite-utils"
    url = f"https://github.com/{full_name}.git"

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        rows = [measure(url, s, root) for s in CloneStrategy]

    header = f"{'strategy':<10} {'seconds':>8} {'size MB':>9} {'files':>7} {'commits readable':>18}"
    print(f"\n{full_name}\n{header}\n{'-' * len(header)}")
    for r in rows:
        print(
            f"{r['strategy']:<10} {r['seconds']:>8.2f} {r['size_mb']:>9.1f} "
            f"{r['files']:>7} {r['commits_readable']:>18}"
        )


if __name__ == "__main__":
    main()
