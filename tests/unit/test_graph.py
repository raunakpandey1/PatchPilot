"""Graph tests — the whole agent, offline.

Everything the graph touches is substituted: a mock HTTP transport for GitHub, a
temp-directory workspace, a local fixture repository instead of a clone, and a
scripted language model. The graph itself runs for real.

That is the payoff of passing dependencies in at build time rather than letting
nodes construct their own.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from patchpilot.agent.deps import AgentDeps
from patchpilot.agent.graph import build_graph, continue_or_halt, memory_checkpointer
from patchpilot.agent.state import AgentState, initial_state, summarize
from patchpilot.llm.base import Usage
from patchpilot.llm.fake import FakeProvider
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.workspace import Workspace

NOW = datetime.now(UTC)

GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}

PYPROJECT = """
[tool.pytest.ini_options]
testpaths = ["tests"]
"""

GOOD_BODY = """
Calling `widget.load()` on a missing file returns None instead of raising.

Steps to reproduce:

```python
widget.load("nope.json")   # expected FileNotFoundError
```

Expected behaviour: an error.
"""


def repo_json(full_name: str = "acme/widget") -> dict:
    return {
        "full_name": full_name,
        "owner": {"login": full_name.split("/")[0]},
        "name": full_name.split("/")[1],
        "default_branch": "main",
        "clone_url": "https://github.com/acme/widget.git",
        "html_url": "https://github.com/acme/widget",
        "language": "Python",
    }


def issue_json(number: int, *, labels=("bug",), assignees=(), comments=2, body=GOOD_BODY) -> dict:
    updated = (NOW - timedelta(days=5)).isoformat()
    return {
        "number": number,
        "title": f"widget.load() returns None for missing files (#{number})",
        "body": body,
        "state": "open",
        "labels": [{"name": n} for n in labels],
        "assignees": [{"login": a} for a in assignees],
        "user": {"login": "someone"},
        "comments": comments,
        "created_at": updated,
        "updated_at": updated,
        "html_url": f"https://github.com/acme/widget/issues/{number}",
    }


@pytest.fixture
def fixture_repo(tmp_path) -> Path:
    """A real git repository the graph can 'clone' from."""
    root = tmp_path / "origin"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text(PYPROJECT)
    (root / "widget.py").write_text("def load(path):\n    return None\n")
    (root / "tests" / "test_widget.py").write_text("def test_load():\n    assert True\n")

    env = {**GIT_ENV, "HOME": str(tmp_path)}
    for args in (["git", "init", "-b", "main"], ["git", "add", "."], ["git", "commit", "-m", "init"]):
        subprocess.run(args, cwd=root, env=env, check=True, capture_output=True)
    return root


def make_deps(tmp_path, fixture_repo, *, issues: list[dict] | None = None, repo_status: int = 200):
    """Wire a complete, offline set of dependencies."""
    issues = [issue_json(1)] if issues is None else issues

    def handler(request: httpx.Request) -> httpx.Response:
        if repo_status != 200:
            return httpx.Response(repo_status, json={"message": "Not Found"})
        if "/issues" in str(request.url):
            return httpx.Response(200, json=issues)
        return httpx.Response(200, json=repo_json())

    return AgentDeps(
        github=GitHubClient(token="fake", transport=httpx.MockTransport(handler)),
        workspace=Workspace(tmp_path / "ws"),
        llm=FakeProvider(),
    )


@pytest.fixture
def deps_factory(tmp_path, fixture_repo, monkeypatch):
    """Patch `clone` so the graph clones the local fixture repository."""
    import patchpilot.agent.nodes.repository as repo_node
    from patchpilot.tools.git import clone as real_clone

    def local_clone(url, destination, **kwargs):
        kwargs.pop("branch", None)
        return real_clone(str(fixture_repo), destination, allow_file_protocol=True, **kwargs)

    monkeypatch.setattr(repo_node, "clone", local_clone)

    def factory(**kwargs):
        return make_deps(tmp_path, fixture_repo, **kwargs)

    return factory


# --- the routing function in isolation --------------------------------------


def test_routing_is_a_pure_function():
    """'When does the agent stop?' is testable with a dictionary, rather than
    only observable by running the whole graph."""
    assert continue_or_halt(AgentState(halted=True)) == "halt"
    assert continue_or_halt(AgentState(halted=False)) == "continue"
    assert continue_or_halt(AgentState()) == "continue"


# --- the happy path ---------------------------------------------------------


def test_graph_runs_end_to_end_and_selects_an_issue(deps_factory):
    graph = build_graph(deps_factory())

    final = graph.invoke(initial_state("run_1", "acme/widget"))

    assert final["visited"] == [
        "analyze_repository", "discover_issues", "rank_issues", "select_issue"
    ]
    assert final["selected_issue"].number == 1
    assert final["snapshot"].is_testable
    assert not final["halted"]
    assert "reproducibility" in final["selection_reason"]


def test_the_highest_ranked_issue_is_selected(deps_factory):
    graph = build_graph(
        deps_factory(
            issues=[
                issue_json(1, labels=("epic",), body="please add a thing", comments=60),
                issue_json(2, labels=("good first issue", "bug")),
                issue_json(3, labels=(), comments=0),
            ]
        )
    )

    final = graph.invoke(initial_state("run_2", "acme/widget"))

    assert final["selected_issue"].number == 2


# --- halting ----------------------------------------------------------------


def test_missing_repository_halts_with_a_reason_not_an_exception(deps_factory):
    """A private or misspelled repository is an outcome, not a crash."""
    graph = build_graph(deps_factory(repo_status=404))

    final = graph.invoke(initial_state("run_3", "acme/nope"))

    assert final["halted"]
    assert "not found" in final["halt_reason"].lower()
    assert final["visited"] == ["analyze_repository"], "later nodes must not run"
    assert final["errors"]


def test_no_open_issues_halts_cleanly(deps_factory):
    graph = build_graph(deps_factory(issues=[]))

    final = graph.invoke(initial_state("run_4", "acme/widget"))

    assert final["halted"]
    assert "no open issues" in final["halt_reason"]
    assert final["visited"] == ["analyze_repository", "discover_issues"]


def test_nothing_actionable_halts_and_says_how_close_it_got(deps_factory):
    """Refusing work is a valid outcome, and the reason has to be specific
    enough to act on."""
    graph = build_graph(
        deps_factory(issues=[issue_json(9, labels=("epic",), body="add feature", comments=80)])
    )

    final = graph.invoke(initial_state("run_5", "acme/widget"))

    assert final["halted"]
    assert "threshold" in final["halt_reason"]
    assert final["selected_issue"] is None
    assert final["ranked_issues"], "ranking still happened; nothing cleared the bar"


def test_assigned_issues_are_never_selected(deps_factory):
    graph = build_graph(deps_factory(issues=[issue_json(1, assignees=("maintainer",))]))

    final = graph.invoke(initial_state("run_6", "acme/widget"))

    assert final["halted"]
    assert final["selected_issue"] is None


# --- state mechanics --------------------------------------------------------


def test_visited_accumulates_rather_than_overwrites(deps_factory):
    """Without an `operator.add` reducer, each node's return would replace the
    list and only the last node would be recorded."""
    graph = build_graph(deps_factory())

    final = graph.invoke(initial_state("run_7", "acme/widget"))

    assert len(final["visited"]) == 4
    assert len(set(final["visited"])) == 4


def test_usage_accumulates_across_nodes():
    from patchpilot.agent.state import accumulate_usage

    assert accumulate_usage(Usage(10, 5, 1.0), Usage(2, 3, 0.5)).total_tokens == 20


def test_summary_describes_where_a_run_got_to(deps_factory):
    graph = build_graph(deps_factory())

    text = summarize(graph.invoke(initial_state("run_8", "acme/widget")))

    assert "acme/widget" in text
    assert "selected #1" in text
    assert "analyze_repository → discover_issues" in text


# --- checkpointing ----------------------------------------------------------


def test_state_survives_into_the_checkpointer(deps_factory):
    """The mechanism Phase 9 needs: a run must be resumable from a different
    process, minutes or hours later."""
    saver = memory_checkpointer()
    config = {"configurable": {"thread_id": "run_resume"}}

    build_graph(deps_factory(), checkpointer=saver).invoke(
        initial_state("run_resume", "acme/widget"), config=config
    )

    # A *different* graph object reads the state back — nothing is held in memory
    # by the graph that ran it.
    recovered = build_graph(deps_factory(), checkpointer=saver).get_state(config)

    assert recovered.values["run_id"] == "run_resume"
    assert recovered.values["selected_issue"].number == 1
    assert recovered.values["visited"] == [
        "analyze_repository", "discover_issues", "rank_issues", "select_issue"
    ]


def test_every_node_produces_a_checkpoint(deps_factory):
    """One checkpoint per step is what makes a crash cost one node's work
    instead of the whole run — which on a free tier is a cost control."""
    saver = memory_checkpointer()
    config = {"configurable": {"thread_id": "run_history"}}
    graph = build_graph(deps_factory(), checkpointer=saver)

    graph.invoke(initial_state("run_history", "acme/widget"), config=config)

    assert len(list(graph.get_state_history(config))) > 4


def test_separate_threads_do_not_share_state(deps_factory):
    """thread_id is what separates one run from another in shared storage."""
    saver = memory_checkpointer()
    graph = build_graph(deps_factory(), checkpointer=saver)

    for thread in ("run_a", "run_b"):
        graph.invoke(
            initial_state(thread, "acme/widget"), config={"configurable": {"thread_id": thread}}
        )

    a = graph.get_state({"configurable": {"thread_id": "run_a"}})
    b = graph.get_state({"configurable": {"thread_id": "run_b"}})

    assert a.values["run_id"] == "run_a"
    assert b.values["run_id"] == "run_b"
