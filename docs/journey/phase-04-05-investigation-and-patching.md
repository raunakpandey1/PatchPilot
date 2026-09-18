# Phases 4–5 — Investigation and patch generation

**Goal:** turn an issue into a diagnosis, and a diagnosis into a minimal patch.
**Date:** 2026-09-18

## 1. What we built

```
selected issue
     │
     ▼
retrieve_context ──► analyze_root_cause ──► plan_fix ──► generate_patch
   (dense, 8)          RootCause schema      FixPlan       CodeEdit list
                       + evidence            + risks       → applied to a copy
```

The first place a model makes a decision. Everything before it stays
deterministic.

| Module | Responsibility |
|---|---|
| [`prompts/investigation.py`](../../src/patchpilot/agent/prompts/investigation.py) | diagnosis prompt, with untrusted content fenced |
| [`nodes/investigation.py`](../../src/patchpilot/agent/nodes/investigation.py) | retrieve, then diagnose, with hallucination checks |
| [`prompts/fixing.py`](../../src/patchpilot/agent/prompts/fixing.py) | plan and patch prompts, region extraction |
| [`nodes/fixing.py`](../../src/patchpilot/agent/nodes/fixing.py) | plan, then generate edits |
| [`tools/patching.py`](../../src/patchpilot/tools/patching.py) | apply to a copy, compute the diff |

## 2. How it works

**Retrieval and diagnosis are separate nodes.** Retrieval is measurable on its
own, so a bad diagnosis can be attributed: *was the right code even in the
prompt?* Fused into one node, that question is unanswerable and every failure
looks like "the model is bad at this".

**The model does exact-text replacement; we compute the diff.** A hunk header
requires line arithmetic, models get it wrong, and a corrupt patch tells you
nothing about whether the fix was right. See
[ADR-015](../adr/ADR-015-exact-text-edits.md).

**Planning and generation are separate calls.** A plan is a paragraph and a file
list — cheap to produce and cheap to reject. Reviewing a diff to discover the
approach was wrong wastes the diff.

**Everything the model claims is checked in code.** The prompt asks it not to
invent file paths; `_citations_outside_context` verifies it did not. The prompt
asks for a minimal fix; the plan is checked against the diagnosis's cited files.

## 3. Why this way

The pattern throughout is: **ask in the prompt, verify in code.** A prompt is a
request, and a request that matters should have a check behind it. Three
concrete instances:

| asked in the prompt | verified in code |
|---|---|
| do not invent file paths | citations must appear in the retrieved context |
| stay within the diagnosis | plan files must be cited files |
| never weaken a test | Phase 8's policy denies skip markers |

## 4. Tradeoffs

- **Two model calls instead of one** for plan + patch. Justified because a plan
  is cheap to reject; on a tighter budget they could be merged.
- **Bounded file regions** rather than whole files. `db.py` is 4,000+ lines. The
  cost is that a fix needing context outside the cited region cannot be written —
  which shows up as the model declining, an honest failure.
- **Exact-text matching is whitespace-sensitive.** Real, and mitigated by showing
  the file verbatim.

## 5. Problems encountered

**The default model had been retired.** `gemini-2.0-flash` returned 404 with the
API naming a successor. Model IDs are not stable; `scripts/list_models.py` and
`patchpilot models` exist because of this.

**Then the replacement returned 503 for minutes** while other models served
normally, and later a 429. Led to the fallback chain —
[ADR-019](../adr/ADR-019-fallback-and-cache.md).

**F-007 — the cache key moved.** Covered in [failures.md](../failures.md).

## 6. Debugging process — the 503

**Symptom.** The first live run halted: `503 UNAVAILABLE`. The retry policy had
done everything right — exponential backoff, four attempts over 38 seconds, a
clean halt with a reason — and it did not help.

**Investigation.** The question was whether the *provider* was down or that
*model* was busy. Testing the same prompt against five models in the same minute:

```
gemini-3.8-flash        503
gemini-3.7-flash        503
gemini-3.6-flash        OK   (10.9 s)
gemini-3.5-flash-lite   OK   ( 1.4 s)
```

**Root cause.** Capacity varies per model, minute to minute. Retrying one model
harder cannot fix that.

**Fix.** A fallback chain that tries models in order — on unavailability only,
never on a bad response, because a malformed request fails identically everywhere.

**Verification.** The Phase 5 run completed on the third model, with five
fallback events including a 429 in the log.

## 7. Tests

48 tests across investigation and fixing. The ones worth noting are about *bad*
model behaviour:

- `test_hallucinated_file_citation_is_caught` — the model cites a file it was
  never shown; the run halts.
- `test_low_confidence_root_cause_stops_the_run` — refusing is the point. A patch
  built on an imagined cause looks like progress.
- `test_a_plan_beyond_the_evidence_is_rejected` — the model went past what it was
  given.
- `test_missing_context_halts_before_calling_the_model` — no tokens spent when
  there is nothing to reason about.
- `test_ambiguous_text_is_refused_rather_than_guessed` — replacing the first of
  two matches would be a plausible-looking wrong fix.

## 8. Metrics

Full detail in [metrics.md](../metrics.md). On `simonw/sqlite-utils` #841:

| | |
|---|---|
| retrieval | top 2 chunks were the two functions named in the issue |
| root cause | **3/3 citations verified exact** against the source |
| patch | 2 edits, 1 file, **+0/−4 lines**, applied cleanly |
| cost | 3 calls, 6,625 tokens |
| repeat run | **0 calls** (cache) |

The diagnosis found the two `if not self.exists(): return` guards *and* cited
`count_where` as the contrast case — a sibling method without the guard, which
does raise. That is the evidence that makes the diagnosis checkable.

**One correct result is not a success rate.** Phase 11 measures that.

## 9. Official documentation

- [Gemini API — Structured output](https://ai.google.dev/gemini-api/docs/structured-output) — `response_schema` and its limits.
- [Gemini API — Function calling](https://ai.google.dev/gemini-api/docs/function-calling) — the general mechanism behind schemas.
- [`difflib`](https://docs.python.org/3/library/difflib.html) — `unified_diff`, used to generate the patch.

## 10. Interview questions

1. Why separate retrieval from diagnosis?
2. How do you stop a model inventing file paths?
3. Why exact-text edits rather than a diff?
4. Why plan and patch as two calls?
5. What happens when the model declines to produce a patch?
6. Your model returned 503 for minutes. What did you do, and what did you *not*
   fall back on?

## 11. STAR story

**S-009** — the retry that worked perfectly and did not help. See the
[story bank](../interview/story-bank.md).
