# ADR-002 — pydantic-settings for configuration, with an optional GitHub token

**Status:** Accepted · **Phase:** 0 · **Date:** 2026-09-09

## Context

PatchPilot needs a GitHub token, a workspace path, timeouts, size budgets and a
log level. These vary per machine and one is a secret. They are read by many
modules across many phases.

A separate question arrived with it: should a missing GitHub token stop the
program from starting?

## Options considered

1. **`os.getenv` at each use site.** No dependency, no ceremony. No validation,
   no types, no single inventory of what is configurable.
2. **A hand-written config module** reading the environment once into a
   dataclass. Better, but type coercion and validation are written by hand and
   drift.
3. **`pydantic-settings`.** Declarative, typed, validated, with `.env` support
   and secret masking built in.
4. **A config file** (YAML/TOML) plus environment overrides. Good for large
   configuration surfaces; more machinery than this needs, and puts secrets in a
   file by default.

## Decision

`pydantic-settings`, in a single [`config.py`](../../src/patchpilot/config.py).

The GitHub token is **optional**, and validated where the higher rate limit is
actually required rather than at import.

## Reasoning

**On the library.** Configuration errors should fail once, at startup, with a
clear message. Pydantic gives typed coercion (`"300"` → `300`), constraints
(`gt=0`), `Literal` for enumerations, and `SecretStr` for masking — all
declaratively. Hand-writing that is a few hundred lines that will be wrong
somewhere.

**On the optional token.** GitHub's API serves public repositories
unauthenticated at 60 requests/hour, versus 5,000 authenticated. Making the
token required means nobody can clone this repository and run `pytest` without
first provisioning a credential. For a project whose purpose is to be read and
learned from, that is a real cost.

So: **required to run for real, optional to import.**

## Tradeoffs

**Against:** a dependency; `settings = Settings()` at module level means
importing anything reads the environment, which tests work around with
`_env_file=None`; validation errors are verbose.

**For:** one inventory of every setting; fail-fast on bad values; secrets masked
in reprs, logs and tracebacks by default.

**The optional-token tradeoff specifically:** the failure moves later, from
startup to the first request that needs the budget. That is worse for
diagnosability, and it is accepted so that the test suite runs for anyone.

## Consequences

- Every setting is declared in one file; adding one elsewhere is a review flag.
- Secrets use `SecretStr`, asserted by
  `test_token_is_not_exposed_by_repr`.
- `.env` is gitignored; `.env.example` is committed.
- Module-level `settings` may need to become a cached factory if import-time
  environment reads become awkward.

## Interview questions

**Q: How do you manage configuration and secrets?**

A single typed settings class reading environment variables with a `.env`
fallback, validated once at startup. Secrets are `SecretStr` so they are masked
wherever they get formatted, and `.env` is gitignored with a committed
`.env.example` as the template.

**Q: Should a missing credential fail at startup or at first use?**

Startup is clearer, and here it would have made the project unrunnable without a
token — GitHub serves public repositories unauthenticated, just at a lower rate
limit. I made it optional to import and validated where the higher limit is
needed. The cost is a later, less obvious failure; the benefit is that anyone
can run the test suite. Which way you go depends on whether the credential is
required for the software to do anything at all.

**Q: What does `SecretStr` actually protect against?**

Accidental disclosure through logs, reprs and tracebacks — which is how tokens
leak in practice, far more often than by someone reading the source. It also
makes reading the real value an explicit `.get_secret_value()` call, which is
greppable in review.

## Behavioural question this answers

> *"Tell me about a tradeoff you made between safety and usability."*

Failing fast on a missing GitHub token would have been the safer default, but it
would have meant nobody could run the test suite without provisioning a
credential first — for an API that serves public data unauthenticated. I made
the token optional to import and validated it at the point the higher rate limit
is actually needed, accepting a later failure in exchange for a project anyone
can run. I recorded the tradeoff in the ADR rather than leaving it implicit.
