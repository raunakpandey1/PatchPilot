# ADR-003 — structlog for event-style logging

**Status:** Accepted · **Phase:** 0 · **Date:** 2026-09-09

## Context

By Phase 7 a single agent run will emit dozens of events across ten components,
several of which repeat inside a retry loop. Debugging requires answering "what
happened during run `abc123`, in order?" — which means every event must carry a
run identifier without every function taking one as a parameter.

## Options considered

1. **`print()`.** Zero setup. No levels, no filtering, no structure.
2. **Standard library `logging`.** Built in, universal, levels and handlers.
   Messages are formatted strings; adding structured context means `extra={...}`
   on every call and a custom formatter.
3. **`structlog`.** Key–value events, context variables that propagate
   automatically, pluggable rendering (console or JSON).
4. **OpenTelemetry logs.** The full observability stack. Correct destination,
   large amount of machinery for Phase 0.

## Decision

`structlog`, rendering to a readable console by default and JSON on demand. It
writes through the standard library's `logging`, so third-party libraries that
use `logging` still work normally.

## Reasoning

The deciding feature is **context variables**. `bind_contextvars(run_id=...)`
once at the start of a run means every subsequent event from any module carries
it. With the standard library that requires threading a logger adapter through
every call site, or a custom filter plus discipline.

The second reason is that a log line becomes queryable data. `duration_s=4.2` as
a field can be averaged; `"in 4.2 seconds"` inside a sentence needs a regular
expression and breaks when someone rewords the message.

OpenTelemetry is the right answer eventually and is planned for Phase 10 for
traces. Logs do not need it yet.

## Tradeoffs

**Against:** a dependency; a second logging concept alongside the standard
library; event names become an interface that cannot be renamed casually.

**For:** automatic run correlation; machine-readable output; one line to switch
between console and JSON; a direct path to correlating logs with traces in Phase
10.

## Consequences

- Event names (`repo_cloned`, `issues_listed`) are treated as a stable
  interface.
- Phase 10 binds `run_id` once per agent run; nothing else needs to change.
- Hot loops must log summaries, not per-item events.

## Interview questions

**Q: Why structured logging over the standard library?**

Mainly automatic context propagation. Binding a `run_id` once and having every
subsequent event carry it is what makes one run's activity recoverable from a
mixed stream — otherwise you thread a logger through every function. Secondly,
key–value events can be filtered and aggregated; formatted sentences need
regular expressions.

**Q: What is the difference between a log, a metric and a trace?**

A log is a discrete event: what happened, when, with what fields. A metric is an
aggregate over time: how often, how long, how many. A trace follows one request
through a system, with timing for each step. For an agent you want all three:
logs to reconstruct a single run, metrics to notice a regression, traces to see
which node spent the time.

**Q: What is the cost of this choice?**

Event names become an interface. Renaming `repo_cloned` silently breaks every
saved query and dashboard built on it, and nothing in the type system stops you.

## Behavioural question this answers

> *"Tell me about a time you invested in tooling before you needed it."*

I set up structured logging in Phase 0, before there was anything complicated to
debug, because the thing it enables — binding a run identifier once and having
every event inherit it — is very hard to retrofit. By Phase 7 there is a retry
loop across ten components, and threading a run id through after the fact would
have meant touching every call site.
