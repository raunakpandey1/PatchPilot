# Structured logging: a log line is an event, not a sentence

## In one sentence

Log key–value pairs instead of sentences, because by Phase 7 one agent run
produces dozens of events across ten components and you will need to filter
them.

## The problem it solves

Compare:

```python
print(f"Cloned {repo} in {duration} seconds")
# Cloned pallets/flask in 4.2 seconds
```

with:

```python
log.info("repo_cloned", repo="pallets/flask", duration_s=4.2)
# 2026-09-17T18:47:48Z [info] repo_cloned  repo=pallets/flask duration_s=4.2
```

The first you can read. The second you can **query**:

- every `repo_cloned` event
- the average `duration_s`
- everything belonging to one run

The first would need a regular expression per message format, and it breaks the
moment someone rewords the sentence.

## How it works, step by step

### 1. The event name is a stable identifier

`repo_cloned` is chosen like a database column name: lowercase, underscored, and
it never changes. The human-readable wording lives in the fields.

### 2. Context binds once and follows everything

This is the part that matters for an agent.

A single run touches the GitHub client, git, the analyzer, retrieval, the
sandbox. You do not want to thread a `run_id` parameter through every function
just so logs can be correlated.

`structlog` uses **context variables** — a per-execution-context store:

```python
structlog.contextvars.bind_contextvars(run_id="run_abc123")

log.info("repo_cloned", repo="pallets/flask", duration_s=4.2)
log.warning("rate_limit_low", remaining=42)
```

Output:

```
[info]    repo_cloned      component=git  duration_s=4.2  repo=pallets/flask  run_id=run_abc123
[warning] rate_limit_low   component=github  remaining=42  run_id=run_abc123
```

`run_id` appears on both, and neither call passed it. Any module that logs
during that run inherits it.

That is what makes "show me everything that happened in run `abc123`" a one-line
filter — and in Phase 10 it is how a log line gets matched to a LangSmith trace.

### 3. One format for humans, another for machines

```python
setup_logging("INFO")                    # coloured, aligned, readable
setup_logging("INFO", json_output=True)  # {"event": "repo_cloned", ...}
```

Same events. Console while developing; JSON when something needs to parse them.

### 4. Levels are a decision about who reads this

| Level | Meaning | Example here |
|---|---|---|
| `DEBUG` | detail for diagnosis | every git command and its duration |
| `INFO` | something meaningful happened | `repository_cloned`, `issues_listed` |
| `WARNING` | survivable, someone should know | `github_rate_limit_low` |
| `ERROR` | this operation failed | a clone that could not be retried |

## In PatchPilot

- [`src/patchpilot/logging.py`](../../src/patchpilot/logging.py) — setup, and
  `merge_contextvars` which does the propagation.
- [`tools/github.py`](../../src/patchpilot/tools/github.py) — `issues_listed`
  logs the count *and* the cache statistics, so the effectiveness of the cache
  is visible in ordinary operation, not only in a benchmark.

## What goes wrong

**Logging secrets.** `log.info("auth", token=settings.github_token)` writes your
token to disk. `SecretStr` masks it — see
[configuration and secrets](configuration-and-secrets.md).

**Formatting the message yourself.** `log.info(f"cloned {repo}")` throws away
the structure and you are back to regular expressions.

**Renaming events.** `repo_cloned` → `repository_cloned` silently breaks every
dashboard and saved query. Treat event names as an interface.

**Logging inside a hot loop.** Per-chunk logging during Phase 3 indexing will
produce tens of thousands of lines and dominate the runtime. Log the summary.

## Interview questions

**Q: What is structured logging and why use it?**

Log events with key–value fields rather than formatted sentences, so logs can be
filtered, counted and aggregated instead of pattern-matched. In an agent it also
lets you bind a `run_id` once and have every subsequent event from any module
carry it, which is what makes a single run's activity recoverable from a mixed
log stream.

**Q: How would you debug a specific agent run that behaved oddly?**

Filter by `run_id`, which is bound at the start of the run and attached to every
event automatically. That gives the ordered sequence of what happened. From
Phase 10 the same `run_id` is on the LangSmith trace, so the logs give the
mechanics and the trace gives the prompts and token counts.

**Q: Logs, metrics, or traces?**

Logs are discrete events — what happened. Metrics are aggregates over time —
how often, how long, how many. Traces are one request's path through a system,
with timing per step. For an agent you need all three: logs to debug one run,
metrics to notice a regression, traces to see which node spent the time.

## Official documentation

- [structlog — Why structured logging?](https://www.structlog.org/en/stable/why.html)
- [structlog — Context variables](https://www.structlog.org/en/stable/contextvars.html) — the `run_id` propagation mechanism.
- [Python `logging` — levels](https://docs.python.org/3/library/logging.html#logging-levels)
