# ADR-015 — Exact-text edits, not model-written diffs

**Status:** Accepted · **Phase:** 5 · **Date:** 2026-09-18

## Context

The agent has to express a code change in a form that can be applied
mechanically, reviewed by a human, and repaired when it fails.

## Options considered

1. **A unified diff written by the model.** The format `git apply` takes.
2. **Whole-file rewrite.** The model returns the complete new file.
3. **Line-range replacement.** "Replace lines 40–44 with this."
4. **Exact-text replacement.** "Replace this specific snippet with this."

## Decision

**Exact-text replacement**, with the unified diff computed by us from the before
and after contents.

## Reasoning

A diff hunk header looks like `@@ -40,7 +40,9 @@`: start line and line count,
before and after. Producing one correctly requires counting context lines and
doing arithmetic — and models are unreliable at exactly that.

The failure is the expensive part. When the arithmetic is wrong, `git apply`
reports a corrupt patch, which tells you **nothing about whether the fix was
right**. A malformed diff and a wrong fix look identical from the outside, so the
repair loop cannot tell which it is repairing.

Exact-text replacement removes the arithmetic entirely. The model reproduces a
snippet it was just shown, which is something models are good at. And
verification becomes a string search with exactly three outcomes:

| outcome | meaning | feedback to the loop |
|---|---|---|
| found once | applies | — |
| not found | the snippet was not copied exactly | "copy the existing code exactly, including indentation" |
| found many times | ambiguous | "include more surrounding lines so it matches exactly once" |

Each is detectable in code, and each produces a message specific enough to act
on. That is what makes the Phase 7 loop able to converge rather than guess.

The real diff is then generated from before/after text, so hunk headers are
correct by construction rather than by hope.

**Why not whole-file rewrite.** Robust, and it costs output tokens proportional
to file size — `sqlite_utils/db.py` is over 4,000 lines. On a free tier that is
prohibitive, and it makes review harder because the diff must be reconstructed
to see what changed. It also invites unrelated reformatting.

**Why not line ranges.** Same arithmetic problem as diffs, plus the ranges go
stale the moment an earlier edit shifts the file.

## Tradeoffs

**Against:**

- **Whitespace sensitivity.** `old_text` must match exactly, so a model that
  normalises indentation fails. Mitigated by showing the file verbatim with a
  numbered gutter it is told not to include.
- **Ambiguity is common.** `if not self.exists():` appears twice in the target
  file. The model must include enough context, and the error message asks for
  precisely that.
- **Sequential edits interact.** A later edit can depend on an earlier one, so
  files are re-read per edit rather than snapshotted.

**For:** no line arithmetic; three distinguishable failure modes; actionable
repair feedback; a correct-by-construction diff; small prompts.

## Consequences

- The patch prompt must show file contents verbatim, which bounds how much can
  be shown — hence extracting regions around cited evidence rather than whole
  files.
- Ambiguous matches are **refused rather than guessed**. Silently replacing the
  first of two occurrences would be a plausible-looking wrong fix, which is the
  worst possible outcome.
- Line numbers are shown for orientation and explicitly excluded from the edit,
  in both the system prompt and the user message.

## Interview questions

**Q: How does your agent express a code change?**

As exact-text replacements — "this snippet becomes this" — and I compute the
unified diff myself from the before and after contents. Models are unreliable at
hunk-header arithmetic, and when they get it wrong `git apply` reports a corrupt
patch, which tells you nothing about whether the fix was right. Reproducing a
snippet it was just shown is something a model does well.

**Q: What does that buy you concretely?**

Three distinguishable failure modes instead of one. The snippet matches once, is
absent, or is ambiguous — each detectable with a string search and each producing
a message the repair loop can act on: "copy it exactly" versus "include more
surrounding lines". With a malformed diff you get "corrupt patch" and no idea
which problem you have.

**Q: Why not just have the model rewrite the whole file?**

Output tokens proportional to file size, on a file that is over 4,000 lines, on
a free tier. It also makes review worse — you have to reconstruct the diff to see
what changed — and invites unrelated reformatting that a reviewer then has to
read past.

**Q: What is the weakness of your approach?**

Whitespace sensitivity, and ambiguity. The snippet must match character for
character including indentation, and in real code a short snippet often appears
more than once. I handle the first by showing the file verbatim and the second by
refusing rather than guessing — replacing the first of two matches silently would
be a plausible-looking wrong fix, which is worse than a clean failure.

## Behavioural question this answers

> *"Tell me about a time you designed around a tool's weakness rather than
> fighting it."*

The natural way for an agent to express a code change is a unified diff, and
models produce broken ones because the hunk headers require line arithmetic.
I could have added a repair loop for malformed diffs. Instead I changed the
representation so the arithmetic never happens: the model does exact-text
replacement, which it is good at, and I compute the diff from the result, which
is something a computer is good at. The payoff was not just fewer failures — it
was that the remaining failures became *distinguishable*, so the repair loop gets
"this snippet appears twice, add more context" instead of "corrupt patch".
