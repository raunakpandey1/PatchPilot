# PatchPilot

An autonomous open-source contribution agent. Given a GitHub repository, it
ranks the open issues, retrieves the relevant code, diagnoses a root cause,
writes a minimal patch, runs the repository's own tests against it in a sandbox,
repairs it when they fail — and stops for a human before anything reaches the
repository.

**Status:** Phases 0–12 complete. The pipeline runs end to end on real issues.
The end-to-end benchmark has not been run — see [what is not
measured](#what-is-not-measured).

```
 issue ──► rank ──► retrieve ──► diagnose ──► plan ──► patch
                                                         │
                     ┌───────────────────────────────────┤
                     │                                   ▼
                     │                          validate in Docker
                     │                                   │
                     └──── repair ◄── failed ───────────┤
                                                    passed
                                                         ▼
                                            policy review ──► human approval
```

## Principles

1. **The repository is untrusted input** — as code (never runs on the host) and
   as text (never treated as instructions).
2. **Nothing consequential without a human.** A policy denial never even reaches
   the approval screen.
3. **Deterministic where possible.** The model is for judgement; code is for
   looking up facts that are already written down.
4. **Measured, not asserted.** Every number comes from a command you can re-run.
   Where something has not been measured, it says so.

## Quick start

Requires Python 3.13, [Poetry](https://python-poetry.org/), Docker, and a free
[Google AI Studio key](https://aistudio.google.com/apikey).

```bash
poetry install
cp .env.example .env          # add PATCHPILOT_GEMINI_API_KEY
poetry run patchpilot doctor  # checks everything needed for a full run
```

```bash
patchpilot issues simonw/sqlite-utils      # rank open issues — no model calls
patchpilot explain simonw/sqlite-utils 841 # why that issue scored what it did
patchpilot index  simonw/sqlite-utils      # build the retrieval index, locally
patchpilot run    simonw/sqlite-utils      # diagnose → patch → test → wait
patchpilot approve run_abc123 approve      # resume, possibly hours later
```

## Documentation

**[docs/](docs/) assumes no prior knowledge of agents, RAG or LangGraph.**
Start at [docs/README.md](docs/README.md).

| | |
|---|---|
| [concepts/](docs/concepts/) | 26 explainers from zero — what an LLM is, what an agent is, embeddings, hybrid search, chunking code, sandboxing, prompt injection, guardrails, MCP, evaluation |
| [journey/](docs/journey/) | the build diary, one entry per phase group |
| [adr/](docs/adr/) | 20 decision records, each with the alternatives and the interview questions it raises |
| [interview/](docs/interview/) | question bank and 11 STAR stories from what actually happened |
| [failures.md](docs/failures.md) | 8 real bugs, with the debugging process |
| [metrics.md](docs/metrics.md) | every measured number, with the command |

## What it does today

| | |
|---|---|
| **GitHub** | ETag conditional requests, incremental sync, Link-header pagination, rate-limit tracking, retries that distinguish "wait" from "give up" |
| **Cloning** | blobless by default, no submodule recursion, hardened git environment, time and size budgets |
| **RAG** | AST chunking on declaration boundaries, local embeddings, Qdrant embedded, hybrid retrieval with weighted rank fusion, cross-encoder reranking |
| **Agent** | LangGraph, 13 nodes, SQLite checkpointing, a bounded repair cycle |
| **Sandbox** | no network, read-only root, memory/PID/CPU limits, dropped capabilities, non-root, external timeout |
| **Guardrails** | a policy engine with allow/require-approval/deny, injection detection, secret scanning |
| **Human** | `interrupt()` + resume; no default, no timeout that approves |
| **Interfaces** | 8 CLI commands, an MCP server exposing read-only tools |

## Findings

**Dense retrieval beat hybrid, and reranking made it worse.** Measured on 192
labelled examples: Recall@5 0.835 dense vs 0.757 hybrid vs 0.775 with a
cross-encoder, which also cost 46× the latency. A weight sweep showed hybrid
converging monotonically towards dense-only, which is the signature of one ranker
adding nothing the other lacked — [ADR-012](docs/adr/ADR-012-hybrid-retrieval.md).

**A shallow clone reads one commit of history.** Blobless reads all of it at ~45%
of a full clone's size. The fastest option was the one that could not do the job.

**A repeat issue fetch costs zero rate-limit budget** with conditional requests,
against two without.

**The sandbox controls are verified, not claimed.** A test inside the container
tries to open a socket to 1.1.1.1 and fails the build if it succeeds.

## What is not measured

The end-to-end benchmark harness exists and is tested; it has not been run,
because free-tier quota is exhausted. So the issue-to-patch rate, first-attempt
fix rate and cost per fix are **unknown**, and no estimate is offered for them.

One correct patch on `sqlite-utils` #841 — a +0/−4 diff removing exactly the two
early returns the diagnosis identified, with all three citations verified against
the source — is a single data point, not a success rate.

## Layout

```
src/patchpilot/
├── models.py        domain vocabulary
├── config.py        one validated settings inventory
├── tools/           github, git, workspace, patching
├── analysis/        deterministic repository characterisation
├── llm/             provider protocol, gemini, fallback, cache, fake
├── rag/             chunking, embeddings, store, retrieval, reranking
├── agent/           state, ranking, nodes, prompts, graph
├── sandbox/         docker runner, output parsing
├── guardrails/      policy engine, injection detection
├── observability/   per-node timing and tokens
├── evaluation/      retrieval and end-to-end benchmarks
├── cli/             8 commands
└── mcp/             read-only tools for other agents

tests/unit/          467 tests, offline, ~17s
tests/integration/   12 tests, real GitHub and real Docker
docs/                concepts, journey, ADRs, failures, metrics, interview prep
```

## Development

```bash
poetry run pytest -m "not integration"   # fast suite
poetry run pytest -m integration         # real GitHub + Docker
poetry run ruff check . && poetry run mypy
```
