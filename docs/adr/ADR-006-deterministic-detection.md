# ADR-006 — Repository analysis is deterministic, with no LLM

**Status:** Accepted · **Phase:** 1 · **Date:** 2026-09-17

## Context

Before working on a repository, PatchPilot must know: what language is it, what
package manager, what is the test command, does it have a linter, a type
checker, CI.

An LLM could answer all of this from the file listing. It is tempting — one
prompt instead of a few hundred lines of parsing.

## Options considered

1. **LLM-based.** Feed it `pyproject.toml`, `tox.ini` and the file tree; ask for
   structured output.
2. **Deterministic parsing.** Read the files and apply explicit rules.
3. **Deterministic with LLM fallback** for unrecognised projects.

## Decision

**Deterministic.** No LLM in Phase 1 at all.

## Reasoning

Every fact here is already written down in the repository. `pyproject.toml`
declares the build backend. `[tool.pytest.ini_options]` declares pytest.
`requires-python` declares the version. Reading a file is free, instant, and
identical every time.

An LLM would cost money (on a free tier, quota we need elsewhere), take a
second, vary between runs, and — the expensive failure — can invent a command
that does not exist.

That last one propagates. If this module reports `pytest --cov` for a project
without `pytest-cov`:

1. Phase 6's sandbox runs it and it fails.
2. Phase 7's debug loop sees a failure and tries to fix the *patch*.
3. Several LLM calls are spent debugging a problem that was never in the code.
4. Phase 11's benchmark records a failure with the wrong cause.

A wrong answer here does not stay here. It is laundered into confident prose
three phases downstream.

There is also a testability argument: deterministic code can be tested by
building a repository with known contents and asserting the exact output. The
15 tests in [`test_analysis.py`](../../tests/unit/test_analysis.py) run offline
in milliseconds. Testing an LLM's answer means either mocking it — proving
nothing — or paying for every test run.

## The general principle

> **Use the LLM for judgement. Use code for lookup.**

"What test runner is this?" is lookup. "Is this issue small enough to be worth
attempting?" is judgement. Phase 2 will be careful about which side of that line
each node sits on.

## Tradeoffs

**Against:**
- Only handles patterns explicitly coded. An unusual project returns `UNKNOWN`.
- Every new ecosystem (Node, Go, Rust) means new parsing rules.
- More code than a prompt.

**For:** free, instant, reproducible, testable offline, and cannot hallucinate.
`UNKNOWN` is an honest answer that callers can act on;
`RepositorySnapshot.is_testable` uses it to refuse work that cannot be
validated.

## Consequences

- Broadening language support is a code change, not a prompt change.
- A repository with no discoverable test command is reported as not testable
  rather than guessed at. The agent refuses work it cannot verify.
- Option 3 (LLM fallback) remains available later, and would need the same
  scepticism: an invented command is worse than `UNKNOWN`.

## Interview questions

**Q: Why not ask an LLM what the test command is?**

Because the answer is written down in the repository — reading a file is free,
instant and deterministic. An LLM costs money, varies between runs, and can
invent a command that does not exist. That last failure is the expensive one: a
fabricated command fails in the sandbox, the debug loop then spends several LLM
calls "fixing" a patch that was never the problem, and the benchmark records the
wrong failure cause.

**Q: Where *is* an LLM the right tool here?**

Where the answer is not written down. "Is this issue well-specified enough to
attempt?", "what is the root cause of this failure?", "what should the fix look
like?" — judgement over ambiguous evidence. The rule I use is: lookup in code,
judgement in the model.

**Q: What happens when your rules do not match a project?**

It reports `UNKNOWN` and `is_testable` is false, so the agent declines the work.
An honest "I don't know" is better than a plausible guess, because the guess
produces a failure whose cause is invisible.

**Q: How do you test this?**

Build small repositories in a temp directory with known files, run the analyzer,
assert the exact output. Fifteen tests, offline, milliseconds. Including
adversarial cases — `src/latest/thing.py` must not be classified as a test file
just because its path contains the letters "test".

## Behavioural question this answers

> *"Tell me about a time you deliberately chose not to use a new technology."*

Building an AI agent, the obvious move for "what kind of project is this?" was
to ask the model. I did not, because every one of those facts is declared in the
repository's own config files, and a model's answer would be slower, cost quota
I do not have, vary between runs, and occasionally invent a test command that
does not exist. That last failure would surface three phases later as a patch
that "failed its tests", sending the debug loop after a bug that was never
there. The principle I settled on and applied through the rest of the project is
that the model is for judgement and code is for lookup.
