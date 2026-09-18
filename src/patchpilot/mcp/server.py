"""Exposing PatchPilot's capabilities over the Model Context Protocol.

What MCP actually solves
------------------------
Before MCP, every AI application invented its own way to describe a tool to a
model, and every tool provider had to write an adapter per application. N tools
times M applications means NxM integrations.

MCP is a wire protocol for that description: a server advertises tools with JSON
Schema, a client discovers and calls them. N + M instead of N x M. That is the
whole value proposition, and it is a real one — the same argument as LSP for
editors, which is where the design comes from.

What MCP does **not** solve
----------------------------
It standardises *how a tool is described and called*. It has nothing to say
about whether calling it is a good idea. Authorization, rate limiting, auditing
and blast radius are all still yours. A server that exposes `delete_repository`
over MCP has exposed `delete_repository`; the protocol adds no safety.

That matters here because PatchPilot's capabilities are genuinely dangerous, and
so this server exposes **only the read-only ones**. Patch generation, commits and
pull requests are deliberately absent — not because MCP could not carry them,
but because a tool an arbitrary client can invoke is a tool with no human in the
loop, and the human is the control.

Why the business logic does not know this file exists
------------------------------------------------------
Every function here is a thin call into code that already existed. That was the
architectural rule from Phase 0 — business logic never imports its transport —
and this module is the test of whether the rule held. It did: nothing under
`agent/`, `rag/` or `tools/` imports anything MCP, and deleting this directory
would change no behaviour.

That is also the honest answer to "when is MCP unnecessary complexity?": when
you have one client, which is most of the time. It earns its place when the
tools are genuinely reusable by clients you do not control.
"""

from __future__ import annotations

import json
from typing import Any

from patchpilot.config import settings
from patchpilot.logging import get_logger
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.workspace import Workspace

log = get_logger("mcp")


def _workspace() -> Workspace:
    return Workspace(settings.workspace_dir)


def _github() -> GitHubClient:
    import subprocess

    from patchpilot.tools.http_cache import FileCache

    token = subprocess.run(
        ["gh", "auth", "token"], capture_output=True, text=True, check=False
    ).stdout.strip() or settings.github_token_value()
    return GitHubClient(token=token, cache=FileCache(_workspace().cache_dir()))


# --- the capabilities, as plain functions -----------------------------------
#
# Each returns JSON-serialisable data. They are ordinary functions rather than
# MCP-decorated ones so they can be unit tested without a protocol, and so the
# protocol layer below stays a translation rather than an implementation.


def analyze_repository(repository: str) -> dict[str, Any]:
    """Clone a repository and describe its structure and toolchain."""
    from patchpilot.analysis.repository import analyze_repository as analyze
    from patchpilot.tools.git import GitRepository, clone

    workspace = _workspace()
    with _github() as github:
        repo = github.get_repository(repository)
        destination = workspace.repo_dir(repository)
        local = (
            GitRepository(destination)
            if (destination / ".git").exists()
            else clone(repo.clone_url, destination)
        )
        snapshot = analyze(repo, local)

    return {
        "repository": snapshot.repository.full_name,
        "commit": snapshot.head_sha,
        "languages": list(snapshot.languages),
        "package_manager": str(snapshot.package_manager),
        "test_runner": str(snapshot.test_runner),
        "test_command": list(snapshot.test_command or ()),
        "source_files": len(snapshot.source_files),
        "test_files": len(snapshot.test_files),
        "is_testable": snapshot.is_testable,
    }


def find_issues(repository: str, limit: int = 10) -> dict[str, Any]:
    """Rank open issues by how attemptable they look. Deterministic."""
    from patchpilot.agent.ranking import rank_issues

    with _github() as github:
        ranked = rank_issues(github.list_issues(repository, limit=200))

    return {
        "repository": repository,
        "total_open": len(ranked),
        "issues": [
            {
                "number": r.issue.number,
                "title": r.issue.title,
                "score": round(r.score, 3),
                "verdict": r.verdict,
                "url": r.issue.html_url,
                "reasoning": r.explain(),
            }
            for r in ranked[:limit]
        ],
    }


def search_repository(
    repository: str, query: str, limit: int = 5, exclude_tests: bool = True
) -> dict[str, Any]:
    """Search an indexed repository's code semantically."""
    from patchpilot.rag.embeddings import LocalEmbeddings
    from patchpilot.rag.retrieval import HybridRetriever
    from patchpilot.rag.store import VectorStore

    workspace = _workspace()
    embeddings = LocalEmbeddings()
    store = VectorStore(dimension=embeddings.dimension, path=workspace.resolve("qdrant"))
    try:
        results = HybridRetriever(store, embeddings).retrieve(
            query, limit=limit, mode="dense",
            repository=repository, exclude_tests=exclude_tests,
        )
        return {
            "query": query,
            "results": [
                {
                    "file": r.chunk.file_path,
                    "lines": f"{r.chunk.start_line}-{r.chunk.end_line}",
                    "symbol": r.chunk.qualified_symbol,
                    "score": round(r.score, 3),
                    "text": r.chunk.text,
                }
                for r in results
            ],
        }
    finally:
        store.close()


def check_patch_policy(diff: str, file_path: str, old_text: str, new_text: str) -> dict[str, Any]:
    """Ask the policy engine whether a proposed edit would be permitted.

    Exposed deliberately: it lets another agent find out what is allowed
    *before* proposing something, which is more useful than being refused after.
    It is also read-only — it decides nothing and changes nothing.
    """
    from patchpilot.guardrails.policy import Policy
    from patchpilot.models import CodeEdit, Patch

    patch = Patch(
        edits=(CodeEdit(file_path=file_path, old_text=old_text, new_text=new_text, reason="check"),),
        diff=diff,
        files_changed=(file_path,),
    )
    policy = Policy()
    objections = policy.check_patch(patch)

    return {
        "decision": str(policy.evaluate_patch(patch)),
        "objections": [str(v) for v in objections],
    }


def scan_for_injection(text: str, source: str = "") -> dict[str, Any]:
    """Scan untrusted text for prompt-injection signals.

    Useful to another agent for the same reason it is useful here — and it
    carries the same caveat, which is stated in the response rather than left
    for the caller to discover.
    """
    from patchpilot.guardrails.injection import scan, summarize

    signals = scan(text, source=source)
    return {
        "summary": summarize(signals),
        "signals": [
            {"pattern": s.pattern_name, "severity": str(s.severity), "excerpt": s.excerpt}
            for s in signals
        ],
        "caveat": (
            "Pattern matching is the weakest defence and is evadable by anyone "
            "who has read the patterns. Treat a clean result as 'nothing obvious', "
            "never as 'safe'."
        ),
    }


TOOLS: dict[str, Any] = {
    "analyze_repository": analyze_repository,
    "find_issues": find_issues,
    "search_repository": search_repository,
    "check_patch_policy": check_patch_policy,
    "scan_for_injection": scan_for_injection,
}

# The schemas advertised to a client. Written out rather than generated, because
# the description is the *interface* — a model chooses a tool by reading it, and
# an auto-generated description reads like a type signature rather than an
# explanation of when to use the thing.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "analyze_repository",
        "description": (
            "Clone a GitHub repository and report its languages, package manager, "
            "test runner and test command. Use before anything else — it reports "
            "whether the repository is testable at all."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"repository": {"type": "string", "description": "owner/name"}},
            "required": ["repository"],
        },
    },
    {
        "name": "find_issues",
        "description": (
            "Rank a repository's open issues by how suitable they are for an "
            "automated fix, with an explanation of each score. Deterministic: "
            "the same input always produces the same ranking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": {"type": "string", "description": "owner/name"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["repository"],
        },
    },
    {
        "name": "search_repository",
        "description": (
            "Search an indexed repository's code by meaning rather than keywords. "
            "Returns file paths, line ranges and the code itself. The repository "
            "must have been indexed first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": {"type": "string"},
                "query": {"type": "string", "description": "Natural language or an error message"},
                "limit": {"type": "integer", "default": 5},
                "exclude_tests": {"type": "boolean", "default": True},
            },
            "required": ["repository", "query"],
        },
    },
    {
        "name": "check_patch_policy",
        "description": (
            "Ask whether a proposed code change would be permitted: allow, "
            "require_approval, or deny, with reasons. Decides nothing and changes "
            "nothing — use it before proposing an edit."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "diff": {"type": "string"},
                "file_path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["file_path", "old_text", "new_text"],
        },
    },
    {
        "name": "scan_for_injection",
        "description": (
            "Scan untrusted text (an issue body, a README) for prompt-injection "
            "signals. Detection is evadable; a clean result means 'nothing "
            "obvious', not 'safe'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "source": {"type": "string", "description": "Where the text came from"},
            },
            "required": ["text"],
        },
    },
]


def call_tool(name: str, arguments: dict[str, Any]) -> str:
    """Dispatch a tool call and return JSON.

    Errors are returned as data rather than raised, because a protocol error and
    a tool that could not do its job are different things to a client — the
    first means the call was malformed, the second is a normal outcome it should
    reason about.
    """
    handler = TOOLS.get(name)
    if handler is None:
        return json.dumps({"error": f"unknown tool {name!r}", "available": sorted(TOOLS)})

    try:
        return json.dumps(handler(**arguments), indent=2, default=str)
    except TypeError as exc:
        return json.dumps({"error": f"bad arguments for {name}: {exc}"})
    except Exception as exc:
        log.warning("mcp_tool_failed", tool=name, error=str(exc))
        return json.dumps({"error": f"{name} failed: {exc}"})


async def serve() -> None:
    """Run the MCP server over stdio.

    The SDK's decorators are untyped, so everything inside this function is
    opaque to mypy. Confined to one function deliberately — the handlers above
    are ordinary typed code, and this is the only untyped seam.
    """
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server import Server

    server: Server = Server("patchpilot")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(**schema) for schema in TOOL_SCHEMAS]

    @server.call_tool()
    async def handle(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
        log.info("mcp_tool_called", tool=name)
        return [types.TextContent(type="text", text=call_tool(name, arguments))]

    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    import asyncio

    asyncio.run(serve())


if __name__ == "__main__":
    main()
