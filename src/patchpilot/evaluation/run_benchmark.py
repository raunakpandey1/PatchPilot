"""Run the end-to-end benchmark.

    poetry run python -m patchpilot.evaluation.run_benchmark \
        --repo simonw/sqlite-utils --limit 20

For each issue: run the full agent, classify the outcome, record the cost. The
oracle is the repository's own test suite, so nothing here needs labelling.

Three things this runner does deliberately
-------------------------------------------
**Issues are pinned, not ranked.** The benchmark passes explicit issue numbers,
so a change to the ranker cannot silently alter which issues were attempted.
Otherwise two runs would be incomparable for a reason unrelated to what was being
measured.

**Approval is bypassed, loudly.** There is no human in a benchmark and nothing is
pushed. `auto_approve` logs a warning on every use precisely so it cannot quietly
become a production path.

**The budget is shared across the whole run.** A single issue that loops cannot
consume the allowance meant for twenty.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.graph import run as run_agent
from patchpilot.agent.graph import sqlite_checkpointer
from patchpilot.config import settings
from patchpilot.evaluation.benchmark import (
    IssueResult,
    Outcome,
    classify,
    summarize_results,
)
from patchpilot.llm import build_provider
from patchpilot.logging import get_logger, setup_logging
from patchpilot.rag.embeddings import LocalEmbeddings
from patchpilot.rag.retrieval import HybridRetriever
from patchpilot.rag.store import VectorStore
from patchpilot.sandbox.docker_runner import DockerSandbox
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.http_cache import FileCache
from patchpilot.tools.workspace import Workspace

log = get_logger("benchmark")


def github_token() -> str | None:
    result = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() or settings.github_token_value()


def select_issues(repository: str, limit: int, workspace: Workspace) -> list[int]:
    """Choose the issues to attempt, best-ranked first.

    Selection happens once, up front, and the numbers are then pinned — so the
    set being measured is fixed even though the ranker that chose it may change.
    """
    from patchpilot.agent.ranking import rank_issues

    with GitHubClient(token=github_token(), cache=FileCache(workspace.cache_dir())) as github:
        ranked = rank_issues(github.list_issues(repository, limit=200))

    attemptable = [r for r in ranked if r.verdict in ("attempt", "maybe")]
    return [r.issue.number for r in attemptable[:limit]]


def run_one(deps: AgentDeps, repository: str, issue_number: int, max_attempts: int) -> IssueResult:
    """Run the agent against one issue and classify what happened."""
    run_id = f"bench_{uuid.uuid4().hex[:8]}"
    started = time.monotonic()

    try:
        with sqlite_checkpointer(Path(settings.checkpoint_db)) as checkpointer:
            final = run_agent(
                deps, repository, run_id=run_id, checkpointer=checkpointer,
                max_debug_attempts=max_attempts, issue_number=issue_number,
            )
        outcome = classify(final)
        note = final.get("halt_reason", "")
    except Exception as exc:
        # An exception here is infrastructure, not the agent judging badly. It is
        # classified as ERROR and excluded from the denominator, because counting
        # it as a failure would make the metric a measure of the machine.
        log.warning("benchmark_issue_errored", issue=issue_number, error=str(exc))
        final, outcome, note = {}, Outcome.ERROR, str(exc)[:200]

    patch = final.get("patch") if final else None
    usage = final.get("usage") if final else None

    return IssueResult(
        issue_number=issue_number,
        outcome=outcome,
        repair_attempts=final.get("debug_attempts", 0) if final else 0,
        tokens=usage.total_tokens if usage else 0,
        model_calls=getattr(deps.llm, "stats", {}).get("calls_made", 0) if deps.llm else 0,
        duration_s=round(time.monotonic() - started, 1),
        files_changed=len(patch.files_changed) if patch else 0,
        lines_changed=(patch.lines_added + patch.lines_removed) if patch else 0,
        model=deps.llm.name,
        note=note[:200],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the PatchPilot benchmark.")
    parser.add_argument("--repo", default="simonw/sqlite-utils")
    parser.add_argument("--limit", type=int, default=20, help="How many issues to attempt")
    parser.add_argument("--max-attempts", type=int, default=3, help="Repair attempts per issue")
    parser.add_argument("--issues", type=int, nargs="*", help="Specific issue numbers")
    parser.add_argument("--output", type=Path, help="Write results as JSON")
    args = parser.parse_args()

    setup_logging("WARNING")
    workspace = Workspace(settings.workspace_dir)

    sandbox = DockerSandbox()
    if not sandbox.available():
        raise SystemExit(
            "Docker is not running. Without it a patch cannot be validated, and an "
            "unvalidated patch is not a result. Start Docker Desktop and retry."
        )

    issues = args.issues or select_issues(args.repo, args.limit, workspace)
    print(f"Benchmarking {len(issues)} issues on {args.repo}: {issues}\n")

    embeddings = LocalEmbeddings()
    store = VectorStore(dimension=embeddings.dimension, path=workspace.resolve("qdrant"))
    if store.count(repository=args.repo) == 0:
        raise SystemExit(
            f"{args.repo} has not been indexed. Run:\n"
            f"  poetry run patchpilot index {args.repo}"
        )

    llm = build_provider()
    results: list[IssueResult] = []

    with GitHubClient(token=github_token(), cache=FileCache(workspace.cache_dir())) as github:
        deps = AgentDeps(
            github=github, workspace=workspace, llm=llm, settings=settings,
            retriever=HybridRetriever(store, embeddings), sandbox=sandbox,
            auto_approve=True,  # no human exists here, and nothing is pushed
        )

        for index, issue_number in enumerate(issues, start=1):
            print(f"[{index}/{len(issues)}] issue #{issue_number}...", flush=True)
            result = run_one(deps, args.repo, issue_number, args.max_attempts)
            results.append(result)
            print(f"    {result.outcome} · {result.tokens} tokens · {result.duration_s}s")

    store.close()

    report = summarize_results(results, repository=args.repo, model=llm.name)
    print("\n" + report.report())

    if args.output:
        args.output.write_text(json.dumps(report.model_dump(mode="json"), indent=2))
        print(f"\nwritten to {args.output}")


if __name__ == "__main__":
    main()
