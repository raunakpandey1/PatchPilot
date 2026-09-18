# Logs, metrics and traces — three different questions

## In one sentence

Logs tell you *what happened*, metrics tell you *how much and how often*, and
traces tell you *where the time went in one specific run* — and you need all
three because none answers the others' question.

## The problem it solves

A run produced a bad patch. Someone asks what happened.

Without instrumentation you have: the final state, and nothing about how it got
there. Which file did retrieval return? Was the right code even in the prompt?
Did the model get it wrong, or did it never see the answer?

Those are different bugs with different fixes, and you cannot tell them apart
after the fact unless you recorded enough at the time.

## How it works, step by step

### Logs — discrete events, in order

```
[info] context_retrieved   run_id=abc123 issue=841 chunks=8 files=2 mode=dense
[info] root_cause_found    run_id=abc123 confidence=high evidence=3 tokens=2731
[warn] sandbox_check       run_id=abc123 check=tests outcome=failed exit_code=1
```

The key property is `run_id` on every line, bound **once** at the start of the
run rather than passed to every function:

```python
structlog.contextvars.bind_contextvars(run_id=run_id, repository=repository)
```

Every event from every module inherits it. That is what makes "show me
everything that happened in run abc123" a one-line filter rather than a
correlation exercise.

### Metrics — aggregates

```
analyze_repository   0.42s
retrieve_context     0.31s
analyze_root_cause   8.20s, 2731 tokens
plan_fix             3.10s, 1800 tokens
generate_patch       5.00s, 4100 tokens
slowest: analyze_root_cause (49% of measured time)
```

**Per node, not per run.** "This run cost 12,000 tokens" is not actionable.
The breakdown says exactly where to look — and in this project the answer has
repeatedly been the repair loop, which is why bounding it mattered more than
optimising any individual prompt.

### Traces — one run's path, with parent/child structure

A trace is the run as a tree: which node called which, how long each took, and —
uniquely — **the actual prompts and responses**. That last part is what logs
and metrics cannot give you, and it is usually what you need when a model
behaved oddly.

LangSmith provides this once `LANGSMITH_API_KEY` is set.

## Why tracing is opt-in here

Tracing sends prompts and responses to a third party. Those prompts contain code
from a repository you do not own.

That should be a decision someone makes deliberately, not a default they
discover later. So it is off unless a key is present, and turning it on is a
line of configuration rather than a code change.

## Debugging an agent that got slower — the worked example

This is the standard interview question, and the answer is a procedure rather
than a guess:

1. **Metrics first.** Per-node timing says *which* node. If the total went from
   20s to 90s and `validate_patch` went from 2s to 70s, you are looking at
   Docker, not the model.
2. **If it is a model node**, check tokens alongside latency. More tokens means
   the prompt grew — probably retrieval returning more, or the repair loop
   accumulating history. Same tokens but more latency means the provider, not
   you.
3. **Check the fallback counter.** In this project a slow run is often the
   primary model returning 503 and the chain falling through — which looks like
   "the agent got slower" and is actually "the provider had a bad afternoon".
4. **Then the trace**, for the specific run, to see the actual prompt.
5. **Then logs**, filtered by `run_id`, for the sequence.

Notice the order: aggregate to specific. Starting with a trace means reading one
run and hoping it is representative.

## In PatchPilot

- [`logging.py`](../../src/patchpilot/logging.py) — structlog with
  `merge_contextvars`.
- [`observability/tracing.py`](../../src/patchpilot/observability/tracing.py) —
  `RunTrace`, per-node timing, LangSmith configuration.
- Every LLM call returns `Usage` with tokens and latency, so accounting never
  depends on a node remembering to measure.

## What goes wrong

**Measuring only totals.** A total tells you there is a problem, never where.

**Instrumenting after the fact.** Adding `run_id` to every log line in month
three means touching every call site. This project bound it in Phase 0, before
there was anything complicated to debug.

**Confusing wall clock with measured time.** A real bug here: the "slowest node"
share divided by wall clock instead of the sum of node durations, and reported
5,565,623%. Different denominators answer different questions.

**Logging inside hot loops.** Per-chunk logging during indexing produces tens of
thousands of lines and dominates the runtime it is supposed to measure.

**Renaming events.** `repo_cloned` → `repository_cloned` silently breaks every
saved query. Event names are an interface.

## Interview questions

**Q: What is the difference between logs, metrics and traces?**

Logs are discrete events — what happened, in order, with fields. Metrics are
aggregates — how long, how often, how much. A trace is one request's path
through the system with timing per step and the prompts and responses attached.
They answer different questions: logs reconstruct a single run, metrics reveal a
regression, traces show where the time and the tokens went inside one run.

**Q: How would you debug an agent that suddenly got slower?**

Start with per-node metrics, not with a trace. That tells you which node, which
usually settles whether the problem is the model, the sandbox, or the network.
If it is a model node, compare tokens to latency: more tokens means the prompt
grew, and equal tokens with worse latency means the provider. In this project I
would also check the fallback counter, because a slow run is often the primary
model returning 503 and the chain falling through — which looks like a slowdown
and is actually degraded service. Only then the trace, for the specific run.

**Q: How do you correlate a log line with a trace?**

A run id bound once into the logging context, so every event from every module
carries it without being passed one, and used as the trace identifier too.
Binding it once is the whole trick — threading it through every function is the
thing you cannot retrofit cheaply.

**Q: Why is tracing opt-in rather than on by default?**

Because it sends prompts and responses — including code from repositories I do
not own — to a third party. That is a decision someone should make deliberately
rather than discover in an invoice or a privacy review.

## Official documentation

- [LangSmith — Tracing concepts](https://docs.smith.langchain.com/observability/concepts) — runs, traces and how LangGraph nodes map onto them.
- [OpenTelemetry — Traces](https://opentelemetry.io/docs/concepts/signals/traces/) — spans, parent/child, and trace context.
- [structlog — Context variables](https://www.structlog.org/en/stable/contextvars.html) — the propagation mechanism.
