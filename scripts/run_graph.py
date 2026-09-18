"""Run the agent end to end against a real repository.

    poetry run python scripts/run_graph.py [owner/name] [--fake]

Clones and analyses the repository, ranks its open issues, picks one, retrieves
the relevant code, and asks the model for a root cause. Requires the repository
to have been indexed first:

    poetry run python scripts/benchmark_retrieval.py <owner/name>

`--fake` uses the scripted provider, so the whole pipeline can be exercised
without spending quota.
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
from patchpilot.llm import build_provider
from patchpilot.llm.fake import FakeProvider
from patchpilot.logging import setup_logging
from patchpilot.rag.embeddings import LocalEmbeddings
from patchpilot.rag.retrieval import HybridRetriever
from patchpilot.rag.store import VectorStore
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.http_cache import FileCache
from patchpilot.tools.workspace import Workspace


def token() -> str | None:
    result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
    return result.stdout.strip() or None


def main() -> None:
    setup_logging(settings.log_level)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    full_name = args[0] if args else "simonw/sqlite-utils"
    use_fake = "--fake" in sys.argv

    workspace = Workspace(settings.workspace_dir)
    run_id = f"run_{uuid.uuid4().hex[:8]}"

    embeddings = LocalEmbeddings()
    store = VectorStore(dimension=embeddings.dimension, path=workspace.resolve("qdrant"))
    retriever = HybridRetriever(store, embeddings)

    llm = FakeProvider() if use_fake else build_provider()
    print(f"run {run_id} · repo {full_name} · model {llm.name}\n")

    with GitHubClient(token=token(), cache=FileCache(workspace.cache_dir())) as github:
        deps = AgentDeps(
            github=github,
            workspace=workspace,
            llm=llm,
            settings=settings,
            retriever=retriever,
        )
        with sqlite_checkpointer(Path(settings.checkpoint_db)) as checkpointer:
            final = run(deps, full_name, run_id=run_id, checkpointer=checkpointer)

    print("\n" + "=" * 72)
    print(summarize(final))
    print("=" * 72)

    if chunks := final.get("retrieved_chunks"):
        print("\nContext given to the model:")
        for scored in chunks:
            print(f"  {scored.score:.3f}  {scored.chunk.location:<34} {scored.chunk.qualified_symbol}")

    if final.get("errors"):
        print("\nErrors:")
        for error in final["errors"]:
            print(f"  - {error}")

    store.close()


if __name__ == "__main__":
    main()
