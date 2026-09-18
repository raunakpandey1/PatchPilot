"""Dependencies the graph's nodes need.

Why this exists
---------------
Nodes need a GitHub client, a workspace, a language model. Those are live
objects with open sockets and file handles, so they cannot live in the agent
state — state gets serialised to the checkpoint database, and a socket does not
survive that.

So dependencies are passed in when the graph is *built*, and each node closes
over them. Two consequences worth noticing:

* **Tests substitute a whole world.** A fake LLM, a mock-transport GitHub client
  and a temp-directory workspace produce a graph that runs end to end offline.
* **The state stays serialisable.** Everything in ``AgentState`` is data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from patchpilot.config import Settings
from patchpilot.config import settings as default_settings
from patchpilot.guardrails.policy import Policy
from patchpilot.llm.base import LLMProvider
from patchpilot.rag.retrieval import HybridRetriever, RetrievalMode
from patchpilot.sandbox.docker_runner import DockerSandbox
from patchpilot.tools.git import CloneStrategy
from patchpilot.tools.github import GitHubClient
from patchpilot.tools.workspace import Workspace


@dataclass
class AgentDeps:
    """Everything the nodes need from the outside world."""

    github: GitHubClient
    workspace: Workspace
    llm: LLMProvider
    # default_factory, not a plain default: a dataclass refuses a mutable
    # default, and Settings is a Pydantic model.
    settings: Settings = field(default_factory=lambda: default_settings)
    clone_strategy: CloneStrategy = CloneStrategy.BLOBLESS

    # How many issues to consider. Fetching every issue of a ten-year-old
    # repository is slow and mostly pointless — they are sorted by recent
    # activity, so the interesting ones are at the front.
    max_issues: int = 100

    # Retrieval. Optional because Phases 1-2 run without it; the investigation
    # node halts with a clear reason rather than crashing when it is absent.
    retriever: HybridRetriever | None = None

    # Dense by default, not hybrid. That is the measured result on this corpus,
    # not the conventional choice — see ADR-012.
    retrieval_mode: RetrievalMode = "dense"

    # Chunks put in front of the model. Kept small deliberately: more context is
    # not better context, and every chunk costs tokens on a free tier.
    retrieval_limit: int = 8

    # Validation. Optional because earlier phases run without Docker; the node
    # halts with a stated reason rather than presenting an unvalidated patch.
    sandbox: DockerSandbox | None = None

    # Lint is on by default and type checking is not: a repository's linter
    # usually passes on its own code, whereas many projects have type errors
    # they tolerate, and failing a patch for pre-existing ones would be wrong.
    validate_lint: bool = True
    validate_types: bool = False

    # The policy engine. Defaults are built in `review_patch`; injecting one
    # lets a test tighten or loosen the rules without touching the node.
    policy: Policy | None = None

    # Bypass human approval. Only for tests and benchmarks, where no human
    # exists and nothing is pushed. Never a production path.
    auto_approve: bool = False
