# ADR-001 — Use a `src/` layout

**Status:** Accepted · **Phase:** 0 · **Date:** 2026-09-09

## Context

Python resolves imports by searching `sys.path`, whose first entry is typically
the current working directory. With the package directly in the project root,
`import patchpilot` finds the folder sitting there — whether or not the package
is correctly configured for installation.

This project will be installed (a CLI in Phase 12, a Hugging Face Space in Phase
13), so packaging being correct is not optional.

## Options considered

1. **Flat layout** — `patchpilot/` at the project root. Conventional, one less
   directory, works immediately without installing.
2. **`src/` layout** — `src/patchpilot/`. Requires an install before imports
   resolve.
3. **Flat layout plus a CI packaging check** — keep it simple locally, catch
   packaging problems in CI with a build-and-install step.

## Decision

`src/` layout.

## Reasoning

With a flat layout, tests can pass while the package is unbuildable. The failure
mode is not "an error"; it is "everything works locally and nothing works when
installed", discovered by a user.

With `src/`, there is no directory in the working directory for Python to pick
up, so imports only resolve through the installed package. Every test run is
therefore also a packaging test.

Option 3 works, but it moves the feedback from milliseconds to a CI round trip,
and it only helps if the CI step exists and is maintained.

## Tradeoffs

**Against:** one more directory level; `poetry install` is required before
anything runs, which surprises newcomers once; marginally longer import paths.

**For:** packaging errors surface immediately; tests exercise the real installed
artifact; no accidental imports of sibling directories.

## Consequences

- `pyproject.toml` needs `packages = [{ include = "patchpilot", from = "src" }]`.
- `poetry install` is a setup step, documented in the README.
- `mypy` and `ruff` are configured with `src = ["src", "tests"]`.

## Interview questions

**Q: Why `src/` layout?**

Without it, `import mypackage` resolves to a directory in the current working
directory, so the test suite passes whether or not packaging is correct. With
`src/`, imports must go through the installed package, so a packaging mistake
fails at test time instead of at release.

**Q: What is the actual failure it prevents?**

Forgetting to declare a subpackage or a data file in your build configuration.
Flat layout: tests pass, `pip install` produces a broken package. `src/` layout:
the import fails the moment you run the tests.

**Q: When would a flat layout be fine?**

A script or a project never distributed as a package. The cost of `src/` is
small but it is not free, and if nothing is ever installed there is nothing to
catch.

## Behavioural question this answers

> *"Tell me about a time you chose a more inconvenient approach for a good
> reason."*

The `src/` layout costs an extra setup step and a slightly longer path, and in
exchange every test run doubles as a packaging test. Given this project ships as
both a CLI and a deployed app, the class of bug it prevents — works locally,
broken when installed — is one that would otherwise be found by a user rather
than by me.
