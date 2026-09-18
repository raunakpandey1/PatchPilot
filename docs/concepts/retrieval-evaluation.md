# Measuring retrieval (without paying anyone to label data)

## In one sentence

For every closed issue, the commit that closed it names the files that actually
had to change — so git has already recorded the correct answer, and retrieval
can be scored against it for free.

## The problem it solves

You improve chunking. Is retrieval better?

The tempting answer is to run a query, read the results, and decide they look
good. That is not a measurement. It cannot be compared across changes, it
flatters whatever you just built, and it makes "hybrid search is better" an
opinion.

A real measurement needs **ground truth**: for query X, which results are
correct? Normally that means paying people to label a dataset, which for a side
project means it does not happen.

## How it works, step by step

### The trick

When a maintainer fixes issue #841, they commit something like:

```
Fix rows_where on missing tables, closes #841
```

That commit touched **exactly the files that had to change**. So:

```
closed issue  →  the commit that closed it  →  files changed
   (query)                                      (correct answer)
```

is a labelled example nobody had to annotate.

It works because the labels are a *side effect of how software is actually
developed*. That makes them honest in a way a hand-built set often is not: they
record what really had to change, not what someone thought ought to be relevant.

### Building the set

1. Fetch closed issues from the API.
2. Walk the git log, extracting issue numbers from commit messages
   (`closes #N`, `fixes #N`, or a bare `#N`).
3. For each match, read the files that commit touched.
4. **Discard commits touching more than six files.** A refactor or a release
   commit would make recall look excellent for the wrong reason: with twenty
   files marked correct, almost anything retrieved is a hit.

### The three metrics, and what each is for

**Recall@K** — of the files that had to change, how many appear in the top K?

> The headline number here. A file that is never retrieved can never be fixed,
> so everything downstream is capped by this.

**Precision@K** — of the K returned, how many were relevant?

> Matters because irrelevant chunks consume context window and mislead the
> model. Recall alone would be maximised by returning everything.

**MRR** — 1 / the position of the first correct result, averaged.

> Cares about how *high* the first good answer lands. With only a few chunks
> fitting in a prompt, a correct result at position 20 is the same as no result.

### Reading them together

They pull against each other, which is the point:

- Return 50 chunks → recall rises, precision falls, prompt fills with noise.
- Return 1 chunk → precision may be perfect, recall collapses.

Reported side by side at K=5 and K=10, so the trade is visible rather than
chosen silently.

## The limitations, stated up front

Every one of these makes the numbers *conservative*, which is the right
direction for a metric to be wrong in.

**The fix commit is one valid answer, not the only one.** A file retrieved that
was not in that commit is scored wrong, even if it was genuinely relevant. So
these are a **lower bound** on real quality.

**Only issues closed by an identifiable commit are usable.** Plenty of issues
are closed as stale, duplicate, or fixed in a commit that never mentions them.
Those are silently excluded, and the surviving set may be skewed towards
issues whose fixes were tidy.

**Scored at file granularity, not chunk.** The labels are files, so scoring
chunks would measure something the labels cannot support. A chunk from the right
file counts as a hit even if it is the wrong function in that file.

**One repository.** A number from one codebase is evidence, not a law.

Stating these matters more than the numbers. A benchmark whose limitations are
undocumented is a benchmark that will eventually be quoted as though it had
none.

## In PatchPilot

- [`evaluation/retrieval.py`](../../src/patchpilot/evaluation/retrieval.py) —
  ground truth construction and the metrics.
- [`scripts/benchmark_retrieval.py`](../../scripts/benchmark_retrieval.py) —
  runs all four configurations.
- Results in [metrics.md](../metrics.md).

## What goes wrong

**Testing on the data you tuned on.** Adjust chunking until the benchmark
improves and the benchmark stops measuring anything except your tuning. The
defence is to state which decisions were made *before* measuring.

**Ignoring how small the sample is.** Forty examples is enough to notice a large
difference and nowhere near enough to resolve a small one. A 2% gap on this set
is noise.

**Reporting only the best configuration.** The comparison is the finding. "It
gets 0.8" means nothing without "and the alternative gets 0.6".

**Believing the number transfers.** It is one repository, one embedding model,
one set of labels.

## Interview questions

**Q: How do you evaluate a RAG system?**

Against ground truth, not by reading results. Here the ground truth is free: for
each closed issue, the commit that closed it names the files that had to change,
so every closed issue is a labelled example git recorded as a side effect of
normal development. I report Recall@K, Precision@K and MRR across four retrieval
configurations so the comparison — not just the number — is the finding.

**Q: Why is recall your headline metric rather than precision?**

Because a file that is never retrieved can never be fixed. Recall caps
everything downstream. Precision still matters, since irrelevant chunks consume
context and mislead the model, which is why both are reported at two values of K
— otherwise you could maximise recall by returning everything.

**Q: What is wrong with your benchmark?**

Several things, and they all make it conservative. The fix commit is one valid
answer, not the only one, so a genuinely relevant file not in that commit scores
as wrong — the numbers are a lower bound. Only issues closed by an identifiable
commit are usable, which may skew towards tidy fixes. Scoring is at file
granularity because that is what the labels support. And it is one repository,
so it is evidence rather than a law.

**Q: How would you improve the evaluation?**

More repositories, to see whether the ranking of strategies holds. Chunk-level
labels for a small hand-checked subset, to measure what file-level scoring
hides. And a held-out split, so tuning against the benchmark cannot quietly turn
into fitting it.

## Official documentation

- [Qdrant — Retrieval quality](https://qdrant.tech/documentation/beginner-tutorials/retrieval-quality/) — precision, recall and how approximate search affects them.
- [GitHub — Linking a pull request to an issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue) — the `closes #N` convention the ground truth relies on.
