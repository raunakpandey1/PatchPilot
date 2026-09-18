"""Run the Phase 2 graph end to end against a real repository.

    poetry run python scripts/run_graph.py [owner/name]
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.graph import run, sqlite_checkpointer
from patchpilot.agent.state import summarize
from patchpilot.config import settings
from patchpilot.llm.fake import FakeProvider
from patchpilot.logging import setup_logging
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.http_cache import FileCache
from patchpilot.tools.workspace import Workspace


def token() -> str | None:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def main() -> None:
    setup_logging("INFO")
    full_name = sys.argv[1] if len(sys.argv) > 1 else "simonw/sqlite-utils"

    workspace = Workspace(settings.workspace_dir)
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    with GitHubClient(token=token(), cache=FileCache(workspace.cache_dir())) as github:
        deps = AgentDeps(
            github=github,
            workspace=workspace,
            # Phase 2 has no LLM-powered node yet; the fake stands in so the
            # dependency is wired and the graph shape is final.
            llm=FakeProvider(),
            settings=settings,
        )
        with sqlite_checkpointer(Path(settings.checkpoint_db)) as checkpointer:
            final = run(deps, full_name, run_id=run_id, checkpointer=checkpointer)

    print("\n" + "=" * 70)
    print(summarize(final))
    print("=" * 70)

    if reason := final.get("selection_reason"):
        print("\nWhy this issue:\n")
        print(reason)


if __name__ == "__main__":
    main()
