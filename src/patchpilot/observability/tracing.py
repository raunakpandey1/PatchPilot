"""Making a run explainable after the fact.

The question this has to answer
--------------------------------
"Run `abc123` produced a bad patch. What happened?"

Answering it needs three different things, and they are not interchangeable:

**Logs** — discrete events. *What happened, in order.* Already provided by
structlog, with `run_id` bound once so every event from every module carries it
(see docs/concepts/structured-logging.md).

**Metrics** — aggregates. *How often, how long, how much.* This module. Per-node
timing and token counts, so "which step is slow" and "where did the money go"
are answerable without reading a transcript.

**Traces** — one run's path through the system, with timing per step and the
parent/child relationship between them. LangSmith provides this for free once
the environment variables are set, and it is the only one of the three that
shows the *prompts and responses*.

Why per-node accounting rather than a total
--------------------------------------------
A run that costs 12,000 tokens tells you nothing actionable. The same run
reported as *root cause 2,700 · plan 1,800 · patch 4,100 · three repairs 3,400*
tells you exactly where to look — and in this project the answer has repeatedly
been "the repair loop", which is why bounding it mattered more than optimising
any single prompt.

LangSmith, and why it is opt-in
--------------------------------
Tracing sends prompts and responses to a third party. For a repository you do
not own, that is a decision somebody should make deliberately rather than
discover. So it is off unless `LANGSMITH_API_KEY` is set, and turning it on is a
line of configuration rather than a code change.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import structlog

from patchpilot.llm.base import Usage
from patchpilot.logging import get_logger

log = get_logger("observability")


@dataclass
class NodeTiming:
    """What one node cost."""

    name: str
    duration_s: float
    tokens: int = 0
    calls: int = 0

    def __str__(self) -> str:
        cost = f", {self.tokens} tokens" if self.tokens else ""
        return f"{self.name}: {self.duration_s:.2f}s{cost}"


@dataclass
class RunTrace:
    """A record of one agent run, assembled as it happens.

    Kept outside the graph state on purpose: it is observability *about* the
    run, not an input to it. Putting it in state would mean every node's output
    re-serialises the whole history on every step.
    """

    run_id: str
    repository: str
    started_at: float = field(default_factory=time.monotonic)
    timings: list[NodeTiming] = field(default_factory=list)

    @property
    def total_duration_s(self) -> float:
        """Wall clock since the run started."""
        return time.monotonic() - self.started_at

    @property
    def measured_duration_s(self) -> float:
        """The sum of the node timings.

        Distinct from wall clock, and the right denominator for "what share of
        the time did this node take". Wall clock includes anything not inside a
        timed block — and using it produced a share of 5,565,623% the first time
        this was run, which is how the distinction got noticed.
        """
        return sum(t.duration_s for t in self.timings)

    @property
    def total_tokens(self) -> int:
        return sum(t.tokens for t in self.timings)

    @property
    def total_calls(self) -> int:
        return sum(t.calls for t in self.timings)

    def record(self, name: str, duration_s: float, usage: Usage | None = None) -> None:
        self.timings.append(
            NodeTiming(
                name=name,
                duration_s=duration_s,
                tokens=usage.total_tokens if usage else 0,
                calls=1 if usage and usage.total_tokens else 0,
            )
        )

    def slowest(self, limit: int = 3) -> list[NodeTiming]:
        return sorted(self.timings, key=lambda t: -t.duration_s)[:limit]

    def most_expensive(self, limit: int = 3) -> list[NodeTiming]:
        return sorted(self.timings, key=lambda t: -t.tokens)[:limit]

    def report(self) -> str:
        """The answer to 'where did the time and the money go?'"""
        lines = [
            f"run {self.run_id} · {self.repository}",
            f"  total: {self.total_duration_s:.1f}s, "
            f"{self.total_tokens} tokens across {self.total_calls} model call(s)",
        ]
        if self.timings:
            lines.append("  by node:")
            lines += [f"    {timing}" for timing in self.timings]
            if slowest := self.slowest(1):
                measured = self.measured_duration_s
                share = slowest[0].duration_s / measured * 100 if measured else 0.0
                lines.append(
                    f"  slowest: {slowest[0].name} ({share:.0f}% of measured time)"
                )
        return "\n".join(lines)


@contextmanager
def bind_run(run_id: str, repository: str) -> Iterator[RunTrace]:
    """Bind the run id to the logging context for the duration of a run.

    Every structlog event emitted inside this block carries `run_id`, from any
    module, without being passed one. That is what makes "show me everything
    that happened in run abc123" a one-line filter — and from here it is also
    the key that joins a log line to its LangSmith trace.
    """
    structlog.contextvars.bind_contextvars(run_id=run_id, repository=repository)
    trace = RunTrace(run_id=run_id, repository=repository)
    try:
        yield trace
    finally:
        log.info(
            "run_trace",
            duration_s=round(trace.total_duration_s, 2),
            tokens=trace.total_tokens,
            calls=trace.total_calls,
            slowest=[t.name for t in trace.slowest(1)],
        )
        structlog.contextvars.unbind_contextvars("run_id", "repository")


@contextmanager
def timed(trace: RunTrace | None, name: str) -> Iterator[None]:
    """Time a block and attribute it to a node."""
    started = time.monotonic()
    try:
        yield
    finally:
        if trace is not None:
            trace.record(name, time.monotonic() - started)


def langsmith_enabled() -> bool:
    """Is tracing configured?

    Opt-in because tracing sends prompts and responses — including code from a
    repository you do not own — to a third party. That should be a deliberate
    decision, not a default someone discovers later.
    """
    return bool(os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"))


def configure_langsmith(project: str = "patchpilot") -> bool:
    """Turn on LangSmith tracing if a key is present. Returns whether it is on."""
    if not langsmith_enabled():
        return False
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_PROJECT", project)
    log.info("langsmith_enabled", project=project)
    return True
