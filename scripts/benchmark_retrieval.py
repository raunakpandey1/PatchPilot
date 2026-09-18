"""Measure retrieval quality against ground truth derived from git history.

For each closed issue, the commit that closed it names the files that actually
had to change. That is the label. No hand annotation.

    poetry run python scripts/benchmark_retrieval.py [owner/name]
"""

from __future__ import annotations

import subprocess
import sys
import time

from patchpilot.analysis.repository import analyze_repository
from patchpilot.config import settings
from patchpilot.evaluation.retrieval import (
    build_ground_truth,
    evaluate_at_multiple_k,
    format_comparison,
)
from patchpilot.logging import setup_logging
from patchpilot.rag.embeddings import LocalEmbeddings
from patchpilot.rag.ingestion import index_repository
from patchpilot.rag.reranking import CrossEncoderReranker
from patchpilot.rag.retrieval import HybridRetriever
from patchpilot.rag.store import VectorStore
from patchpilot.tools.git import GitRepository, clone
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.http_cache import FileCache
from patchpilot.tools.workspace import Workspace


def token() -> str | None:
    out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
    return out.stdout.strip() or None


def main() -> None:
    setup_logging("WARNING")
    full_name = sys.argv[1] if len(sys.argv) > 1 else "simonw/sqlite-utils"
    workspace = Workspace(settings.workspace_dir)

    with GitHubClient(token=token(), cache=FileCache(workspace.cache_dir())) as github:
        repository = github.get_repository(full_name)
        destination = workspace.repo_dir(full_name)
        local = (
            GitRepository(destination)
            if (destination / ".git").exists()
            else clone(repository.clone_url, destination)
        )
        snapshot = analyze_repository(repository, local)

        print(f"Repository: {snapshot.summary()}\n")

        print("Building ground truth from closed issues + fix commits...")
        closed = github.list_issues(full_name, state="closed", limit=400)
        examples = build_ground_truth(closed, local)

    print(f"  {len(closed)} closed issues fetched")
    print(f"  {len(examples)} usable labelled examples (issue -> files that changed)\n")
    if not examples:
        print("No ground truth could be derived. Stopping.")
        return

    print("Sample labels:")
    for example in examples[:3]:
        print(f"  #{example.issue_number}: {example.title}")
        print(f"      -> {', '.join(example.relevant_files)}")
    print()

    embeddings = LocalEmbeddings()
    store = VectorStore(dimension=embeddings.dimension, path=workspace.resolve("qdrant"))

    print("Indexing (first run downloads a 67 MB embedding model)...", flush=True)
    started = time.monotonic()
    result = index_repository(snapshot, store, embeddings)
    print(f"  {result.summary()}\n")

    retriever = HybridRetriever(store, embeddings)
    reranker = CrossEncoderReranker()

    configurations = [
        ("dense", None),
        ("sparse", None),
        ("hybrid", None),
        ("hybrid", reranker),
    ]

    # One retrieval pass per configuration, scored at both K values. Doing it
    # the other way round doubles the cross-encoder work, which dominates.
    by_k: dict[int, list] = {5: [], 10: []}
    for mode, rr in configurations:
        print(f"  evaluating {mode} + {rr.name if rr else 'no rerank'}...", flush=True)
        for score in evaluate_at_multiple_k(
            examples, retriever, repository=full_name, mode=mode, reranker=rr, ks=(5, 10)
        ):
            by_k[score.k].append(score)

    for k, scores in by_k.items():
        print(f"\n=== k={k}, {len(examples)} examples, file-level scoring ===")
        print(format_comparison(scores))

    print(f"\nTotal benchmark time: {time.monotonic() - started:.0f}s")
    store.close()


if __name__ == "__main__":
    main()
