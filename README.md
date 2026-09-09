# PatchPilot

An autonomous open-source contribution agent. Given a GitHub repository, it
finds an actionable issue, investigates it against the codebase, proposes a
patch, validates that patch in a sandbox, and — only with human approval —
opens a pull request.

**Status:** Phase 0 (foundations). Not yet functional.

## Principles

1. **The repository is untrusted input.** Cloned code is never executed on the
   host, and never trusted as instructions.
2. **Nothing consequential happens without a human.** Branch, commit and PR
   creation are gated on explicit approval.
3. **Deterministic where possible.** The LLM is used for judgement, not for
   looking up facts that are written down in the repo.
4. **Measured, not asserted.** Every claim about how well it works comes from
   a benchmark run, not from a demo.

## Setup

Requires Python 3.13 and [Poetry](https://python-poetry.org/).

```bash
poetry env use /opt/homebrew/bin/python3.13
poetry install
cp .env.example .env      # fill in if you have a GitHub token
poetry run pytest
```

## Layout

```
src/patchpilot/      package (src/ layout — imports resolve through the install)
tests/unit/          fast, no network, no Docker
tests/integration/   real services; marked `integration`
docs/adr/            architecture decision records
```

Run the fast suite only:

```bash
poetry run pytest -m "not integration"
```

## Configuration

All settings come from environment variables prefixed `PATCHPILOT_`, or a
local `.env`. See [.env.example](.env.example) for the full list, and
[src/patchpilot/config.py](src/patchpilot/config.py) for types and defaults.
