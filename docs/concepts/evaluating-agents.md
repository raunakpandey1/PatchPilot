# Evaluating an agent

## In one sentence

An agent that produced a convincing demo has told you nothing; the only useful
question is what fraction of real tasks it completes, measured against an oracle
that cannot be argued with.

## The problem it solves

You can always make an agent look good. Pick a favourable issue, run it until it
works, record the screen. That is a demo, and a demo cannot answer the two
questions that matter: *does it work in general*, and *did my change make it
better?*

Both need numbers. Numbers need ground truth.

## How it works, step by step

### Pick an oracle that cannot be argued with

For PatchPilot the oracle is **the repository's own test suite**. A patch either
makes the tests pass or it does not. That is the same standard a maintainer
applies, it is not a matter of opinion, and nobody has to label anything.

The retrieval layer uses a different free oracle: for a closed issue, the commit
that closed it names the files that had to change. Git recorded the answer as a
side effect of normal development — see
[retrieval evaluation](retrieval-evaluation.md).

**Finding a free oracle is most of the work of evaluation.** If you are paying
humans to label, you will build a small dataset once and never refresh it.

### Measure stages separately

PatchPilot measures retrieval on its own *and* end to end. That is not
duplication — it is what makes a failure attributable.

When a patch is wrong there are two possibilities: the right code was never
retrieved, or it was retrieved and the model reasoned badly. Those need different
fixes. Without a separate retrieval number you cannot tell them apart, and every
failure looks like "the model is bad at this".

### Distinguish failures, do not collapse them

A single success rate hides what to work on:

```
fixed                    the patch passed the tests
patch_failed_tests       a patch was produced, and it was wrong
no_patch                 the model declined or produced nothing
patch_did_not_apply      the edits did not match the file
no_root_cause            the investigation was not actionable
no_context               retrieval found nothing
policy_blocked           the patch was refused on safety grounds
error                    infrastructure — not the agent's fault
```

An agent failing mostly at `no_context` has a retrieval problem. One failing at
`patch_failed_tests` has a generation problem. Same headline rate, completely
different next week.

### State the denominator

Infrastructure errors are excluded from PatchPilot's denominator. A Docker
daemon that was down says nothing about the agent, and counting it as a failure
turns the metric into a measure of the laptop.

Which denominator you chose must be visible in the report, because "40%" means
different things over 5 attempted and 5 attempted-of-12.

### Report cost per success, not per attempt

Cost per attempt flatters a system that fails cheaply. What you pay for is a
fix. Reporting both makes the difference visible.

### Say when the sample is too small

Five issues cannot distinguish a 40% success rate from a 60% one. PatchPilot's
report prints a warning below 20 examples, because a number without its
uncertainty invites over-reading — including by the person who produced it.

## What goes wrong — and did, here, twice

Both of this project's evaluation bugs produced **numbers**, not errors. That is
what makes measurement bugs the dangerous kind.

**Labels the system cannot satisfy.** Ground truth included test files, while
the retriever was configured to exclude tests — so recall was capped below 1.0
by construction, at a different level per example
([failures.md F-005](../failures.md)). Nothing failed; it would have printed
0.62 and been optimised against for a week.

**A metric that depended on an unrelated parameter.** The number of chunks
fetched was derived from K, so "Recall@5" meant different things in different
runs. Caught only because the same configuration was measured twice and gave
0.817 and 0.695 ([F-006](../failures.md)).

The two habits that caught both: **print your labels before trusting them**, and
**measure the same thing a second way before publishing it**.

Others worth knowing:

**Tuning on the test set.** Adjust chunking until the benchmark improves and the
benchmark now measures your tuning. State which decisions were made before
measuring.

**Reporting only the winner.** "It gets 0.84" means nothing without "and the
alternative gets 0.76". The comparison is the finding.

**Omitting the model.** With a fallback chain a run can be answered by a
different model than the one configured. A rate without the model that produced
it is not comparable with anything, including itself next week.

## In PatchPilot

- [`evaluation/retrieval.py`](../../src/patchpilot/evaluation/retrieval.py) —
  retrieval, with ground truth from git history.
- [`evaluation/benchmark.py`](../../src/patchpilot/evaluation/benchmark.py) —
  end to end, against the test suite.
- [metrics.md](../metrics.md) — every measured number with the command that
  produced it.

## Interview questions

**Q: How do you know your agent works?**

By measuring it against an oracle that cannot be argued with. Here that is the
repository's own test suite: a patch either makes the tests pass or it does not.
I report distinct outcomes rather than one success rate, because an agent that
never retrieves the right code has a different problem from one whose patches
fail — same headline number, completely different work.

**Q: Why measure retrieval separately when you have an end-to-end number?**

Attribution. When a patch is wrong, either the right code was never in the
prompt or the model reasoned badly, and those have different fixes. Without a
separate retrieval number every failure looks like "the model is bad at this".
Measuring stages independently is what makes a pipeline debuggable rather than
just observable.

**Q: Where do you get ground truth without paying for labels?**

From artefacts the development process already produces. For retrieval, the
commit that closed an issue names the files that had to change — git recorded
the answer as a side effect. For the end-to-end result, the repository's tests.
Finding a free oracle is most of the work, because a hand-labelled set gets
built once and never refreshed.

**Q: What is wrong with your benchmark?**

Several things, and they all push the numbers down rather than up. The fix
commit is one valid answer, not the only one, so a genuinely relevant file that
was not in it scores as wrong. Only issues closed by an identifiable commit are
usable, which may skew towards tidy fixes. It is one repository. And the sample
is small enough that differences under roughly twenty points are not
distinguishable — which the report says on its own output rather than leaving to
the reader.

## Official documentation

- [LangSmith — Evaluation concepts](https://docs.smith.langchain.com/evaluation/concepts) — datasets, evaluators and comparing runs.
- [SWE-bench](https://www.swebench.com/) — the standard benchmark for this exact task, and a useful comparison for how ground truth is constructed at scale.
