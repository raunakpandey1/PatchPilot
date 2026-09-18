# PatchPilot

An autonomous open-source contribution agent. Given a GitHub repository, it
finds an actionable issue, investigates it against the codebase, proposes a
patch, validates that patch in a sandbox, and — only with human approval —
opens a pull request.

**Status:** Phase 2 of 13 complete. It picks an issue; it cannot yet fix one. See [the roadmap](#roadmap).

```
GitHub repo ──► analyse ──► rank issues ──► retrieve code ──► root cause
                                                                   │
        PR ◄── human approval ◄── review ◄── test in sandbox ◄── patch
                                                  │                │
                                                  └── debug loop ──┘
```

## Principles

1. **The repository is untrusted input** — as code (never runs on the host) and
   as text (never treated as instructions).
2. **Nothing consequential without a human.** Branch, commit and PR creation are
   gated on explicit approval.
3. **Deterministic where possible.** The model is for judgement; code is for
   looking up facts that are already written down.
4. **Measured, not asserted.** Every claim about how well it works comes from a
   benchmark run, with the command recorded.

## Documentation

**[docs/](docs/) is written for someone who knows nothing about agents, RAG or
LangGraph.** Start at [docs/README.md](docs/README.md).

| | |
|---|---|
| [concepts/](docs/concepts/) | the ideas, from zero — what an LLM is, what an agent is, LangGraph state and reducers, checkpointing, structured output, RAG, rate limits, untrusted input |
| [journey/](docs/journey/) | the build diary, one file per phase |
| [adr/](docs/adr/) | every real decision, the alternatives, and the interview questions it raises |
| [interview/](docs/interview/) | question bank and STAR stories from what actually happened |
| [failures.md](docs/failures.md) | real bugs, real root causes |
| [metrics.md](docs/metrics.md) | every measured number, with the command |

## Setup

Requires Python 3.13 and [Poetry](https://python-poetry.org/).

```bash
poetry env use python3.13
poetry install
cp .env.example .env      # optional: a GitHub token raises 60 req/hr to 5,000
poetry run pytest -m "not integration"
```

## Try it

```bash
# Compare clone strategies on any public repo
poetry run python scripts/benchmark_clone.py simonw/sqlite-utils

# Measure what conditional requests save
poetry run python scripts/benchmark_github.py simonw/sqlite-utils

# Run the agent: clone, analyse, rank 77 issues, pick one — with reasons
poetry run python scripts/run_graph.py simonw/sqlite-utils
```

A CLI arrives in Phase 12.

## What works today

- **GitHub client** — conditional requests (ETag), incremental sync, Link-header
  pagination, rate-limit tracking, retries that distinguish "wait" from "give
  up". [`tools/github.py`](src/patchpilot/tools/github.py)
- **Cloning** — blobless by default, submodules never recursed, hardened git
  environment, time and size budgets. [`tools/git.py`](src/patchpilot/tools/git.py)
- **Workspace confinement** — every path resolved through symlinks and refused
  if it escapes. [`tools/workspace.py`](src/patchpilot/tools/workspace.py)
- **Repository analysis** — languages, package manager, test command, file
  classification, entirely deterministic.
  [`analysis/repository.py`](src/patchpilot/analysis/repository.py)
- **The agent graph** — LangGraph with typed state, halt-with-a-reason routing,
  and SQLite checkpoints that let a run resume in a different process.
  [`agent/graph.py`](src/patchpilot/agent/graph.py)
- **Issue ranking** — six weighted factors plus hard blockers, fully
  explainable, zero model tokens. [`agent/ranking.py`](src/patchpilot/agent/ranking.py)
- **LLM abstraction** — a two-method protocol with Gemini and scripted
  implementations, so 121 tests run offline and free. [`llm/`](src/patchpilot/llm/)

## Three findings so far

**A shallow clone reads one commit of history.** Blobless reads all of it at
~45% of a full clone's size. The fastest option was the one that could not do
the job — [ADR-004](docs/adr/ADR-004-blobless-clone.md).

**A repeat issue fetch costs zero rate-limit budget** with conditional requests,
against 2 without. Finding that out also uncovered a bug where a warm cache
silently returned 21% fewer issues — [failures.md F-002](docs/failures.md).

**Of 77 open issues on the target repo, 10 are worth an agent attempting** —
ranked in under 3 ms for zero model tokens, each with a printable derivation.
Determinism here is what makes later retrieval improvements measurable at all —
[ADR-010](docs/adr/ADR-010-deterministic-ranking.md).

## Roadmap

| Phase | | Status |
|---|---|---|
| 0 | Foundations | ✅ |
| 1 | GitHub & repository analysis | ✅ |
| 2 | LangGraph + LLM provider abstraction | ✅ |
| 3 | Repository RAG (hybrid retrieval, Qdrant) | |
| 4 | Issue investigation & root cause | |
| 5 | Fix planning & code generation | |
| 6 | Docker sandbox | |
| 7 | Agentic debug loop | |
| 8 | Guardrails & security | |
| 9 | Human in the loop | |
| 10 | Observability | |
| 11 | Evaluation benchmark | |
| 12 | CLI & MCP | |
| 13 | Live deployment | |

## Layout

```
src/patchpilot/      package (src/ layout — imports resolve through the install)
├── models.py        domain vocabulary
├── errors.py        typed failures
├── config.py        one validated settings inventory
├── tools/           adapters: github, git, workspace, http_cache
├── analysis/        deterministic repository characterisation
├── llm/             provider protocol + gemini + scripted fake
└── agent/           state, reducers, ranking, nodes, graph

tests/unit/          121 tests, offline, ~7s
tests/integration/   6 tests, real GitHub, marked `integration`
docs/                concepts, journey, ADRs, interview prep
scripts/             the benchmarks behind docs/metrics.md
```

## Development

```bash
poetry run pytest -m "not integration"   # fast suite
poetry run pytest -m integration         # real API (needs `gh auth login`)
poetry run ruff check .
poetry run mypy
```
