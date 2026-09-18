# Phase 0 — Foundations

**Goal:** a project skeleton where every line is understood.
**Commit:** `5208e72` · **Date:** 2026-09-09

## 1. What we built

A Python project with nothing in it yet — but with the four things every later
phase depends on:

| Piece | File | Job |
|---|---|---|
| Package layout | [`src/patchpilot/`](../../src/patchpilot/) | imports resolve through the install |
| Configuration | [`config.py`](../../src/patchpilot/config.py) | one typed, validated inventory of settings |
| Logging | [`logging.py`](../../src/patchpilot/logging.py) | events with fields, and automatic run context |
| Tests | [`tests/`](../../tests/) | fast offline suite, separate slow suite |

## 2. How it works

`poetry install` installs the package from `src/` in editable mode. Importing
`patchpilot.config` constructs a `Settings` object once, reading environment
variables, then `.env`, then the declared defaults — and raising immediately if
anything is invalid. `setup_logging()` configures structlog so every event
carries whatever context has been bound to the current execution context.

## 3. Why this way

Each choice has its own ADR: [`src/` layout](../adr/ADR-001-src-layout.md),
[pydantic-settings](../adr/ADR-002-pydantic-settings.md),
[structlog](../adr/ADR-003-structlog.md).

The through-line: all three are cheap now and painful to retrofit. Adding a
`run_id` to every log line in Phase 10 would mean touching every call site.

## 4. Alternatives

- **Flat layout** instead of `src/` — simpler, but tests pass whether or not the
  package is installable.
- **`os.getenv` everywhere** — no dependency, no validation, no inventory.
- **`print()`** — no levels, no filtering, no structure.
- **`uv` instead of Poetry** — faster, and would need installing. Poetry was
  already here and install speed is not the bottleneck on a project this size.

## 5. Tradeoffs

The `src/` layout means `poetry install` is required before anything runs, which
surprises newcomers exactly once. `settings = Settings()` at module level means
importing anything reads the environment, which tests work around by passing
`_env_file=None`. Both are accepted and written down rather than discovered
later.

## 6. Problems encountered

None. Phase 0 is setup; the interesting failures start in Phase 1.

## 7. Tests

Seven tests in [`test_config.py`](../../tests/unit/test_config.py):

- defaults let a fresh checkout run with no `.env`
- a relative workspace path resolves against the project root, not the current
  directory
- environment variables override defaults
- **the token does not appear in `repr(settings)`** — accidental disclosure
  through logs and tracebacks is how tokens actually leak
- an invalid log level fails at startup
- a zero timeout is rejected

The test that matters most is the `repr` one. It is a one-line test for a class
of incident.

## 8. Metrics

| metric | value |
|---|---|
| tests | 7 |
| suite runtime | < 1 s |
| Python | 3.13.3 |

## 9. Official documentation

- [src layout vs flat layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/) — why the extra directory earns its place.
- [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — sources and precedence.
- [structlog — Why structured logging?](https://www.structlog.org/en/stable/why.html) — the case for events over sentences.

## 10. Interview questions

1. Why `src/` layout? What bug does it catch?
2. Why commit `poetry.lock` for an application?
3. Should a missing credential fail at import or at first use? Argue both.
4. What does `SecretStr` protect against that a code review would not?
5. What is the difference between a log, a metric and a trace?

Answers are in the [concepts](../concepts/) docs and the ADRs.

## 11. STAR story

None. Nothing meaningful went wrong, and inventing one would defeat the purpose
of the story bank.
