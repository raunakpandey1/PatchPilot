# ADR-010 — Rank issues deterministically, not with an LLM

**Status:** Accepted · **Phase:** 2 · **Date:** 2026-09-18

## Context

A repository has dozens or hundreds of open issues. Most are unsuitable for an
automated agent: already assigned, open-ended feature requests, unreproducible,
or long arguments about design. Something has to choose.

On a real run against `simonw/sqlite-utils`, 77 open issues came back. Ten were
worth attempting.

## Options considered

1. **Ask the model to rank them.** One prompt with all the issues, or one call
   per issue.
2. **Deterministic scoring** over observable properties, with explicit weights.
3. **Hybrid** — deterministic filter to a shortlist, then a model for the final
   choice.
4. **First open issue with a `good first issue` label.** Crude, and would work
   surprisingly often.

## Decision

**Deterministic scoring**, with every score carrying its own derivation.
Hard blockers (assigned, pull request, `wontfix`, empty body) short-circuit
before any scoring happens.

## Reasoning

**Cost.** Ranking 200 issues means either 200 calls or one very long prompt. On
a free tier that is quota spent before any actual work begins, every run.

**Stability, which matters more.** An LLM ranker gives two different orderings
on two runs. That does not just make ranking unreliable — it makes every *other*
measurement impossible. If Phase 3 improves retrieval, the benchmark should show
it. With a wobbling ranker, the agent worked on different issues each run and
the comparison is meaningless. **Determinism upstream is a precondition for
measuring anything downstream.**

**Explainability.** "Why was #412 skipped?" is answered by printing the factor
table. Asking an LLM to justify itself produces a plausible rationalisation,
which is not the same as the reason it used.

**Blockers as a gate, not a penalty.** An issue already assigned to a maintainer
scores exactly zero, not "high, minus a bit". Opening a competing pull request
wastes a maintainer's time — a social constraint, enforced in code rather than
requested in a prompt. There is a test that an otherwise perfect assigned issue
still scores 0.0.

This is the same principle as [ADR-006](ADR-006-deterministic-detection.md):
**lookup in code, judgement in the model.** Whether an issue is assigned is
lookup. Whether the proposed fix is correct is judgement — and stays with the
model.

## What is actually scored

Six weighted factors, reproducibility weighted highest:

| factor | weight | why |
|---|---:|---|
| reproducibility | 3.0 | a bug we cannot reproduce is one we cannot verify a fix for |
| labels | 2.0 | maintainers signal availability and scope with labels |
| scope | 1.5 | crude proxy for size, treated as crude — bands, not a slope |
| clarity | 1.0 | vague titles correlate with vague issues |
| discussion | 1.0 | some comments mean confirmation; many mean disagreement |
| staleness | 1.0 | year-old issues often no longer apply |

Reproducibility is heaviest because an unverifiable patch is worse than no
patch: it looks like progress.

## Tradeoffs

**Against:**

- **Heuristics miss nuance** a human reads instantly. An issue saying "trivial
  one-line fix, just change X to Y" scores no higher than any other prose.
- **The weights are guesses.** They are a starting point, not a result.
- **Gameable**, in principle, by an issue written to match the heuristics.

**For:** free, instant, identical every run, and fully explainable.

The weights being guesses is the interesting part. Because the ranker is
deterministic, Phase 11 can measure how well `verdict == "attempt"` predicts
actual fix success and tune them *against evidence*. With an LLM ranker there
would be nothing stable to tune.

Observed already on real data: issues #399 and #430 scored `attempt` despite
being 1,220 and 1,556 days stale. The staleness weight looks too low. That is a
hypothesis to test in Phase 11, not something to fix by intuition now.

## Consequences

- The thresholds (`attempt` ≥ 0.65, `maybe` ≥ 0.45) live in one function and are
  expected to change once measured.
- Every selection carries an explanation, which becomes part of Phase 9's
  approval screen.
- A hybrid (option 3) stays available: the deterministic scorer produces a
  shortlist and a model picks from it. That would be a small addition, and the
  determinism would still bound the cost.

## Interview questions

**Q: Why not let the model rank the issues?**

Cost, stability and explainability. Cost: 200 issues is 200 calls or one very
long prompt, every run, on a free tier. Stability matters more — a model ranker
orders differently each run, so when I improve retrieval in Phase 3 the agent
works on different issues and the benchmark comparison is meaningless.
Determinism upstream is what makes anything downstream measurable. And a factor
table answers "why was this skipped" directly, whereas asking a model to justify
itself gives you a plausible story rather than its actual reason.

**Q: Your weights are made up. Is that not the same problem?**

They are guesses, and I say so in the ADR. The difference is that they are
*stable* guesses I can test. Phase 11 measures how well the `attempt` verdict
predicts real fix success and tunes the weights against that — a one-line change
with a measurable effect. A model's implicit weights cannot be tuned or even
inspected. I already have a hypothesis from real data: two issues scored
`attempt` despite being over three years stale, which suggests the staleness
weight is too low.

**Q: Where would you still use the model here?**

Judgement over ambiguous evidence. "Is this issue's description self-consistent?"
or "does the proposed approach in the comments actually apply?" are genuine
judgement calls. A hybrid — deterministic shortlist, model picks from the top
ten — would bound the cost while adding that judgement, and the code is
structured so that is an addition rather than a rewrite.

**Q: Why is an assigned issue scored zero rather than penalised?**

Because it is a gate, not a preference. Someone is already working on it, and
opening a competing pull request wastes a maintainer's time. If it were a
penalty, an otherwise excellent issue could outscore it and still get picked.
There is a test asserting a perfect-but-assigned issue scores exactly 0.0.

## Behavioural question this answers

> *"Tell me about a time you made a decision that made future work possible."*

Ranking issues with an LLM would have been less code and looked more impressive.
I used deterministic scoring instead, and the reason was not cost — it was that
an LLM ranker returns a different order every run, so when I later improved
retrieval the agent would be working on different issues and I would have no way
to tell whether the change helped. Determinism upstream is what makes anything
downstream measurable. The tradeoff is that my weights are guesses, so I wrote
them as one function with explicit numbers and a plan to tune them against Phase
11's measured success rate — which is only possible because they are stable.
