"""The command-line interface.

Design notes
------------
**Every command that spends money says so first.** The free tier is the binding
constraint on this project, so `run` prints the call budget and the cache state
before doing anything, and a `--dry-run` exists that does everything except call
a model.

**Failures print what to do, not what went wrong.** "Docker daemon unreachable"
is a description; "Start Docker Desktop, or pass --no-sandbox" is an
instruction. The second is what someone at a terminal needs.

**Approval is a separate command, not a prompt.** `run` stops at the approval
point and exits; `approve` resumes it. That is not a UI decision — it is the
shape the checkpointing already has, and pretending otherwise by blocking on
`input()` would throw away the ability to approve hours later from a different
machine.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from patchpilot.config import settings
from patchpilot.logging import setup_logging

app = typer.Typer(
    name="patchpilot",
    help="An autonomous open-source contribution agent.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _github_token() -> str | None:
    """Borrow the gh CLI's token rather than requiring one to be stored."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10, check=False
        )
        return result.stdout.strip() or settings.github_token_value()
    except (OSError, subprocess.SubprocessError):
        return settings.github_token_value()


@app.command()
def analyze(
    repository: Annotated[str, typer.Argument(help="owner/name")],
) -> None:
    """Clone a repository and describe it. No model calls."""
    setup_logging("WARNING")
    from patchpilot.analysis.repository import analyze_repository
    from patchpilot.tools.git import GitRepository, clone
    from patchpilot.tools.github import GitHubClient
    from patchpilot.tools.http_cache import FileCache
    from patchpilot.tools.workspace import Workspace

    workspace = Workspace(settings.workspace_dir)
    with console.status(f"analysing {repository}..."):
        with GitHubClient(token=_github_token(), cache=FileCache(workspace.cache_dir())) as gh:
            repo = gh.get_repository(repository)
            destination = workspace.repo_dir(repository)
            local = (
                GitRepository(destination)
                if (destination / ".git").exists()
                else clone(repo.clone_url, destination)
            )
            snapshot = analyze_repository(repo, local)

    table = Table(show_header=False, box=None)
    table.add_row("repository", snapshot.repository.full_name)
    table.add_row("commit", snapshot.head_sha[:8])
    table.add_row("languages", ", ".join(snapshot.languages) or "unknown")
    table.add_row("package manager", str(snapshot.package_manager))
    table.add_row("test runner", str(snapshot.test_runner))
    table.add_row("test command", " ".join(snapshot.test_command or ()) or "[red]not found[/red]")
    table.add_row("source files", str(len(snapshot.source_files)))
    table.add_row("test files", str(len(snapshot.test_files)))
    table.add_row(
        "testable",
        "[green]yes[/green]" if snapshot.is_testable else "[red]no — cannot validate a patch[/red]",
    )
    console.print(Panel(table, title=f"[bold]{repository}[/bold]"))


@app.command()
def issues(
    repository: Annotated[str, typer.Argument(help="owner/name")],
    limit: Annotated[int, typer.Option(help="How many to show")] = 10,
    all_verdicts: Annotated[bool, typer.Option("--all", help="Include skipped issues")] = False,
) -> None:
    """Rank a repository's open issues. Deterministic — no model calls."""
    setup_logging("WARNING")
    from patchpilot.agent.ranking import rank_issues
    from patchpilot.tools.github import GitHubClient
    from patchpilot.tools.http_cache import FileCache
    from patchpilot.tools.workspace import Workspace

    workspace = Workspace(settings.workspace_dir)
    with console.status(f"fetching issues for {repository}..."):
        with GitHubClient(token=_github_token(), cache=FileCache(workspace.cache_dir())) as gh:
            ranked = rank_issues(gh.list_issues(repository, limit=200))

    shown = ranked if all_verdicts else [r for r in ranked if r.verdict != "skip"]

    table = Table(title=f"{repository}: {len(ranked)} open issues")
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("verdict")
    table.add_column("title", overflow="ellipsis", max_width=60)
    for result in shown[:limit]:
        colour = {"attempt": "green", "maybe": "yellow", "skip": "dim"}[result.verdict]
        table.add_row(
            str(result.issue.number), f"{result.score:.2f}",
            f"[{colour}]{result.verdict}[/{colour}]", result.issue.title,
        )
    console.print(table)

    counts: dict[str, int] = {}
    for result in ranked:
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
    console.print("  ".join(f"{verdict}: {count}" for verdict, count in counts.items()))


@app.command()
def explain(
    repository: Annotated[str, typer.Argument(help="owner/name")],
    number: Annotated[int, typer.Argument(help="Issue number")],
) -> None:
    """Show why an issue scored the way it did. No model calls."""
    setup_logging("WARNING")
    from patchpilot.agent.ranking import rank_issue
    from patchpilot.tools.github import GitHubClient
    from patchpilot.tools.http_cache import FileCache
    from patchpilot.tools.workspace import Workspace

    workspace = Workspace(settings.workspace_dir)
    with GitHubClient(token=_github_token(), cache=FileCache(workspace.cache_dir())) as gh:
        ranked = rank_issue(gh.get_issue(repository, number))

    console.print(Panel(ranked.explain(), title=f"issue #{number}"))


@app.command()
def models() -> None:
    """List the models the configured API key can reach."""
    from google import genai

    if settings.gemini_api_key is None:
        console.print("[red]PATCHPILOT_GEMINI_API_KEY is not set.[/red]")
        console.print("Get a free key at https://aistudio.google.com/apikey")
        raise typer.Exit(1)

    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    table = Table(title="available models")
    table.add_column("model")
    table.add_column("input", justify="right")
    table.add_column("output", justify="right")

    usable = [
        model for model in client.models.list()
        if model.name and "generateContent" in (model.supported_actions or [])
    ]
    for model in sorted(usable, key=lambda m: m.name or ""):
        table.add_row(
            (model.name or "").replace("models/", ""),
            f"{model.input_token_limit or 0:,}",
            f"{model.output_token_limit or 0:,}",
        )
    console.print(table)
    console.print(f"configured: [bold]{settings.llm_model}[/bold]")


@app.command()
def doctor() -> None:
    """Check that everything needed for a full run is present."""
    setup_logging("ERROR")
    from patchpilot.observability.tracing import langsmith_enabled
    from patchpilot.sandbox.docker_runner import DockerSandbox

    table = Table(title="patchpilot doctor", show_header=True)
    table.add_column("check")
    table.add_column("status")
    table.add_column("note")

    def row(name: str, ok: bool, note: str, *, required: bool = True) -> None:
        if ok:
            status = "[green]ok[/green]"
        else:
            status = "[red]missing[/red]" if required else "[yellow]optional[/yellow]"
        table.add_row(name, status, note)

    row("GitHub token", _github_token() is not None,
        "gh auth login, or PATCHPILOT_GITHUB_TOKEN (60 req/hr without one)", required=False)
    row("Gemini API key", settings.gemini_api_key is not None,
        "https://aistudio.google.com/apikey — needed for investigation onward")
    row("Docker", DockerSandbox().available(),
        "Start Docker Desktop — needed to validate patches")
    row("Vector index", (settings.workspace_dir / "qdrant").exists(),
        "run `patchpilot index <repo>` first")
    row("LangSmith tracing", langsmith_enabled(),
        "set LANGSMITH_API_KEY to record traces", required=False)

    console.print(table)
    console.print(
        f"\nmodel: [bold]{settings.llm_model}[/bold]  "
        f"fallbacks: {', '.join(settings.llm_fallback_models)}  "
        f"call budget: {settings.llm_max_calls}  "
        f"cache: {'on' if settings.llm_cache_enabled else 'off'}"
    )


@app.command()
def index(
    repository: Annotated[str, typer.Argument(help="owner/name")],
) -> None:
    """Build the retrieval index for a repository. No model calls — embeddings run locally."""
    setup_logging("WARNING")
    from patchpilot.analysis.repository import analyze_repository
    from patchpilot.rag.embeddings import LocalEmbeddings
    from patchpilot.rag.ingestion import index_repository
    from patchpilot.rag.store import VectorStore
    from patchpilot.tools.git import GitRepository, clone
    from patchpilot.tools.github import GitHubClient
    from patchpilot.tools.http_cache import FileCache
    from patchpilot.tools.workspace import Workspace

    workspace = Workspace(settings.workspace_dir)
    with GitHubClient(token=_github_token(), cache=FileCache(workspace.cache_dir())) as gh:
        repo = gh.get_repository(repository)
        destination = workspace.repo_dir(repository)
        local = (
            GitRepository(destination)
            if (destination / ".git").exists()
            else clone(repo.clone_url, destination)
        )
        snapshot = analyze_repository(repo, local)

    embeddings = LocalEmbeddings()
    store = VectorStore(dimension=embeddings.dimension, path=workspace.resolve("qdrant"))
    console.print("[dim]embedding runs locally on CPU; the first run downloads a 67 MB model[/dim]")
    with console.status(f"indexing {repository}..."):
        result = index_repository(snapshot, store, embeddings)
    store.close()

    console.print(f"[green]{result.summary()}[/green]")


@app.command()
def approve(
    run_id: Annotated[str, typer.Argument(help="The paused run to resume")],
    decision: Annotated[str, typer.Argument(help="approve | reject | request_changes")],
    note: Annotated[str, typer.Option(help="Reason, shown in the record")] = "",
) -> None:
    """Resume a paused run with a decision.

    A separate command rather than a prompt inside `run`, because the state is
    checkpointed: the decision can come hours later, from a different machine.
    """
    setup_logging("WARNING")
    from langgraph.types import Command

    from patchpilot.agent.graph import build_graph, sqlite_checkpointer

    if decision not in ("approve", "reject", "request_changes"):
        console.print(f"[red]unknown decision {decision!r}[/red]")
        console.print("Use one of: approve, reject, request_changes")
        raise typer.Exit(1)

    deps = _build_deps(use_sandbox=True)
    with sqlite_checkpointer(Path(settings.checkpoint_db)) as checkpointer:
        graph = build_graph(deps, checkpointer=checkpointer)
        config = {"configurable": {"thread_id": run_id}}

        if graph.get_state(config).created_at is None:
            console.print(f"[red]no run found with id {run_id}[/red]")
            raise typer.Exit(1)

        final = graph.invoke(Command(resume={"decision": decision, "note": note}), config=config)

    console.print(f"[bold]{final.get('approval_status')}[/bold] — {final.get('approval_note') or note}")


def _build_deps(*, use_sandbox: bool):  # type: ignore[no-untyped-def]
    from patchpilot.agent.deps import AgentDeps
    from patchpilot.llm import build_provider
    from patchpilot.rag.embeddings import LocalEmbeddings
    from patchpilot.rag.retrieval import HybridRetriever
    from patchpilot.rag.store import VectorStore
    from patchpilot.sandbox.docker_runner import DockerSandbox
    from patchpilot.tools.github import GitHubClient
    from patchpilot.tools.http_cache import FileCache
    from patchpilot.tools.workspace import Workspace

    workspace = Workspace(settings.workspace_dir)
    embeddings = LocalEmbeddings()
    store = VectorStore(dimension=embeddings.dimension, path=workspace.resolve("qdrant"))

    return AgentDeps(
        github=GitHubClient(token=_github_token(), cache=FileCache(workspace.cache_dir())),
        workspace=workspace,
        llm=build_provider(),
        settings=settings,
        retriever=HybridRetriever(store, embeddings),
        sandbox=DockerSandbox() if use_sandbox else None,
    )


@app.command()
def run(
    repository: Annotated[str, typer.Argument(help="owner/name")],
    issue: Annotated[int | None, typer.Option(help="Issue number; otherwise the best-ranked one")] = None,
    no_sandbox: Annotated[bool, typer.Option("--no-sandbox", help="Skip validation (patch will be unverified)")] = False,
    max_attempts: Annotated[int, typer.Option(help="Repair attempts before giving up")] = 3,
) -> None:
    """Run the agent: pick an issue, diagnose it, patch it, test it, then stop for approval."""
    setup_logging(settings.log_level)
    from patchpilot.agent.graph import run as run_graph
    from patchpilot.agent.graph import sqlite_checkpointer
    from patchpilot.agent.state import summarize
    from patchpilot.observability.tracing import bind_run, configure_langsmith

    run_id = f"run_{uuid.uuid4().hex[:8]}"
    configure_langsmith()

    deps = _build_deps(use_sandbox=not no_sandbox)
    console.print(
        f"[dim]run {run_id} · model {settings.llm_model} · "
        f"budget {settings.llm_max_calls} calls · cache "
        f"{'on' if settings.llm_cache_enabled else 'off'}[/dim]"
    )
    if no_sandbox:
        console.print("[yellow]--no-sandbox: the patch will not be validated[/yellow]")

    with bind_run(run_id, repository):
        with sqlite_checkpointer(Path(settings.checkpoint_db)) as checkpointer:
            final = run_graph(
                deps, repository, run_id=run_id, checkpointer=checkpointer,
                max_debug_attempts=max_attempts, issue_number=issue,
            )

    console.print(Panel(summarize(final), title=f"[bold]{run_id}[/bold]"))

    patch = final.get("patch")
    if patch is not None and patch.diff:
        console.print(Syntax(patch.diff, "diff", theme="ansi_dark", line_numbers=False))

    if final.get("approval_status") == "pending" and not final.get("halted"):
        console.print(
            f"\n[bold]Waiting for your decision.[/bold]\n"
            f"  patchpilot approve {run_id} approve\n"
            f"  patchpilot approve {run_id} reject --note 'why'"
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
