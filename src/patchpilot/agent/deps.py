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
from patchpilot.llm.base import LLMProvider
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
