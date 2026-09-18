# Phases 6–7 — Sandbox and the debug loop

**Goal:** find out whether a patch actually works, and try again when it does
not.
**Date:** 2026-09-18

## 1. What we built

```
generate_patch ──► validate_patch ──► record_failure ──┬─ passed ──► review
                        ▲                              │
                        │                              ├─ repair ──► repair_patch ─┐
                        └──────────────────────────────┴─ give up ──► END          │
                                                                                   │
                        └──────────────────────────────────────────────────────────┘
```

| Module | Responsibility |
|---|---|
| [`sandbox/docker_runner.py`](../../src/patchpilot/sandbox/docker_runner.py) | the container and its controls |
| [`sandbox/parsing.py`](../../src/patchpilot/sandbox/parsing.py) | output → structured failures |
| [`nodes/validation.py`](../../src/patchpilot/agent/nodes/validation.py) | run the checks |
| [`nodes/debugging.py`](../../src/patchpilot/agent/nodes/debugging.py) | repair, and the routing that bounds the loop |

## 2. How it works

**The test suite is the oracle.** Not a model's opinion of the patch. This is
the step that turns "the patch looks right" into "the tests pass with it
applied".

**Install and run are different trust levels.** Installing dependencies needs
network *and* executes arbitrary code — `setup.py` runs. So it happens once at
image build; test runs then have no network at all.
[ADR-016](../adr/ADR-016-docker-sandbox.md).

**The loop is bounded three ways** — attempt count, progress detection, and a
call budget one layer down. Nothing in a model produces "I give up".
[ADR-017](../adr/ADR-017-bounded-debug-loop.md).

**The real failure output is fed back, never a summary.** Otherwise the loop
reasons about its own description of reality.

## 3. Why this way

The interesting decisions are all about **what counts as success**:

| situation | decision | why |
|---|---|---|
| sandbox could not run | **not** a pass | "we could not tell" ≠ "it works" |
| pytest exit code 5 | ERROR | no tests collected means nothing was verified |
| no linter configured | SKIPPED, counts as ok | a repo without a linter has not failed linting |
| parser says passed, exit code 1 | FAILED | the exit code decides; parsing only enriches |

Each of these is a place where the permissive answer would silently approve bad
patches.

## 4. Tradeoffs

- **A container is not a VM.** Shared kernel; a kernel exploit escapes. Stated in
  the code and the docs rather than implied away.
- **Image builds are slow** (~16 s trivial, longer real) and amortised by reuse.
- **Progress detection can stop one attempt early.** Accepted: on a free tier,
  three saved calls beat an unlikely third-attempt success.
- **The unit suite got slower** as these phases landed — real Qdrant instances,
  real git repositories, real embedding. Still offline and under 20 s.

## 5. Problems encountered

**F-008 — the parser could not read our own test command's output.** The one
worth reading. Covered below and in [failures.md](../failures.md).

Two smaller ones:

- `networkmode` instead of `network_mode` in the build call — a typo the type
  checker could not catch because the SDK is untyped.
- A one-off native crash (`recursive_mutex lock failed`) during a full test run,
  not reproducible across three subsequent runs. Recorded rather than explained,
  because claiming a root cause I do not have would be worse than saying it
  happened once.

## 6. Debugging process — F-008

**Symptom.** The first real sandbox run built an image, ran the tests, and
returned `outcome=PASSED, exit_code=0` — with **`passed_count=0`**, while the
captured output plainly said `1 passed in 0.01s`.

**Investigation.** The parser looked for pytest's summary line and required the
decorated form:

```python
if line.startswith("=") and PYTEST_COUNT.search(line):
```

That is what pytest prints by default. Under `-q --no-header` it prints the terse
form with no decoration:

```
1 passed in 0.01s
```

And `-q --no-header` is exactly what **our own repository analyzer generates**,
because a terse suite is easier to read.

**Root cause.** Two components written days apart, each correct alone. The
analyzer chose flags that make output compact; the parser was written against the
default format. Nothing connected them.

**Fix.** Accept both forms. The terse pattern requires a trailing duration, so
prose containing "passed" does not match — there is a test for that.

**Verification.** Three parser tests, plus six live Docker tests that previously
failed.

**The generalisable part:** when one component chooses a tool's flags and another
parses its output, they are coupled whether or not they import each other. The
test that catches it is the one running the *real* command against the *real*
parser — which is what the integration suite is for.

## 7. Tests

| suite | count | notable |
|---|---:|---|
| parsing | 16 | truncated output must not claim success; a summary inside test output must not confuse it |
| sandbox (stubbed) | 15 | every container control asserted individually |
| debug loop routing | 21 | the bound is inclusive; identical failures stop early |
| debug loop cycle | 11 | drives the real loop; asserts it terminates three ways |
| sandbox (live Docker) | 6 | network, filesystem, user id, timeout |

The live network test is the important one:

```python
socket.create_connection(('1.1.1.1', 53), timeout=5)
raise AssertionError('NETWORK WAS REACHABLE — the sandbox is not isolating')
```

Passing `network_disabled=True` is a claim about my code. That test is a claim
about Docker's behaviour.

## 8. Metrics

[metrics.md](../metrics.md). Sandbox: **6/6 live integration tests**, 59 s total,
~16 s image build, ~0.7 s test execution.

The four controls proven by live test: no network egress, host filesystem
invisible, non-root execution, timeout enforced.

## 9. Official documentation

- [Docker — Resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
- [Docker — Security](https://docs.docker.com/engine/security/) — capabilities and the kernel caveat, stated by Docker itself.
- [pytest — exit codes](https://docs.pytest.org/en/stable/reference/exit-codes.html) — why 5 is not success.

## 10. Interview questions

1. How do you run untrusted tests safely, and what does it not protect against?
2. Why are install and run separate trust levels?
3. What stops the debug loop running forever — and why three bounds?
4. How do you decide two failures are "the same"?
5. Why is a sandbox error not a pass?
6. Why is pytest exit code 5 an error rather than success?

## 11. STAR story

**S-010** — a green test suite that reported zero tests passed. See the
[story bank](../interview/story-bank.md).
