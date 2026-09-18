"""Measuring whether retrieval actually works.

The problem with evaluating RAG
-------------------------------
You can always make retrieval *look* good. Ask a question, read the five chunks
it returned, decide they seem relevant. That is not a measurement — it is a
demo, and it cannot tell you whether a change helped.

A real measurement needs **ground truth**: for a given query, which chunks
*should* have come back? Usually that means paying humans to label a dataset.

Where the labels come from here, for free
------------------------------------------
Git already recorded the answer.

When a maintainer fixes issue #841, they commit with a message like
``Fix rows_where on missing tables, closes #841``. That commit names **exactly
the files that had to change**. So:

    closed issue  →  the commit that closed it  →  the files it touched

is a labelled example that nobody had to annotate. The issue text is the query;
the changed files are the correct answer.

This works because the labels are a side effect of how software is actually
developed, which makes them honest in a way a hand-built set often is not — they
record what really had to change, not what someone thought ought to be relevant.

What is measured
----------------
* **Recall@K** — of the files that actually had to change, what fraction appear
  in the top K results? For this system Recall matters most: a file that is
  never retrieved can never be fixed. This is the headline number.
* **Precision@K** — of the K results, what fraction were relevant? Matters
  because irrelevant chunks consume context window and mislead the model.
* **MRR** — Mean Reciprocal Rank: 1/position of the first correct result. It
  cares about how *high* the first good answer lands, which matters when only
  the top few chunks fit in a prompt.

Known limitations, stated up front
----------------------------------
* The fix commit is one *valid* answer, not the only one. A file the agent
  retrieves that was not in the commit is scored as wrong even if it was
  genuinely relevant. So these numbers are a **lower bound**.
* Commits touching many files (refactors, releases) are excluded — they would
  make recall look good for the wrong reason.
* **Test files are excluded from the labels.** A fix commit usually touches both
  the source file and its test, but retrieval is configured to exclude tests —
  so leaving them in the ground truth would cap recall below 1.0 by
  construction, and the cap would vary per example. Found by inspecting the
  labels rather than by a failing test; see docs/failures.md F-005.
* Measured on one repository. A number from one codebase is evidence, not a law.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from patchpilot.analysis.repository import is_test_path
from patchpilot.logging import get_logger
from patchpilot.models import Issue
from patchpilot.rag.reranking import Reranker
from patchpilot.rag.retrieval import HybridRetriever, RetrievalMode
from patchpilot.tools.git import GitRepository

log = get_logger("eval.retrieval")

# "closes #841", "fixes #12", "resolves #7", or a bare "#841".
CLOSES_ISSUE = re.compile(
    r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*#(\d+)|#(\d+)", re.IGNORECASE
)

# A commit touching more than this is a refactor, a merge or a release. Counting
# it would inflate recall: with twenty files marked correct, almost anything
# retrieved looks right.
MAX_FILES_PER_FIX = 6


class RetrievalExample(BaseModel):
    """One labelled example: an issue, and the files that actually changed."""

    model_config = ConfigDict(frozen=True)

    issue_number: int
    query: str
    relevant_files: tuple[str, ...]
    commit_sha: str

    @property
    def title(self) -> str:
        return self.query.splitlines()[0][:80] if self.query else ""


class RetrievalScore(BaseModel):
    """Results for one retrieval configuration."""

    model_config = ConfigDict(frozen=True)

    mode: str
    reranker: str
    k: int
    examples: int
    recall_at_k: float
    precision_at_k: float
    mrr: float
    median_latency_ms: float

    def as_row(self) -> str:
        return (
            f"{self.mode:<16} {self.reranker:<18} {self.recall_at_k:>9.3f} "
            f"{self.precision_at_k:>11.3f} {self.mrr:>7.3f} {self.median_latency_ms:>9.1f}"
        )


def build_ground_truth(
    closed_issues: Sequence[Issue],
    repository: GitRepository,
    *,
    max_commits: int = 2000,
    max_files_per_fix: int = MAX_FILES_PER_FIX,
    exclude_test_files: bool = True,
) -> list[RetrievalExample]:
    """Derive labelled examples from git history.

    Walks recent commits, extracts issue numbers from their messages, and pairs
    each closed issue with the files its fixing commit touched.
    """
    by_number = {issue.number: issue for issue in closed_issues}
    examples: dict[int, RetrievalExample] = {}

    for commit in repository.log(max_count=max_commits):
        referenced = {
            int(first or second) for first, second in CLOSES_ISSUE.findall(commit.subject)
        }
        for number in referenced:
            issue = by_number.get(number)
            # Already have one, or this commit does not correspond to a known
            # closed issue. The first (most recent) commit wins.
            if issue is None or number in examples:
                continue

            changed = [f for f in repository.files_changed_in(commit.sha) if f.endswith(".py")]
            # The size check runs on the *full* change: a commit touching twelve
            # files is a refactor whether or not most of them were tests.
            if not changed or len(changed) > max_files_per_fix:
                continue

            # Labels must match what retrieval is allowed to return. Retrieval
            # excludes tests, so a test file in the ground truth is a target
            # that can never be hit — it would cap recall below 1.0 by
            # construction, differently for every example.
            files = tuple(f for f in changed if not (exclude_test_files and is_test_path(f)))
            if not files:
                continue

            examples[number] = RetrievalExample(
                issue_number=number,
                query=issue.text,
                relevant_files=files,
                commit_sha=commit.sha,
            )

    log.info("ground_truth_built", examples=len(examples), issues_considered=len(by_number))
    return sorted(examples.values(), key=lambda e: e.issue_number)


def evaluate_retrieval(
    examples: Sequence[RetrievalExample],
    retriever: HybridRetriever,
    *,
    repository: str,
    mode: RetrievalMode = "hybrid",
    reranker: Reranker | None = None,
    k: int = 5,
    candidate_pool: int = 30,
    exclude_tests: bool = True,
) -> RetrievalScore:
    """Score one retrieval configuration at a single K."""
    return evaluate_at_multiple_k(
        examples, retriever, repository=repository, mode=mode, reranker=reranker,
        ks=(k,), candidate_pool=candidate_pool, exclude_tests=exclude_tests,
    )[0]


def evaluate_at_multiple_k(
    examples: Sequence[RetrievalExample],
    retriever: HybridRetriever,
    *,
    repository: str,
    mode: RetrievalMode = "hybrid",
    reranker: Reranker | None = None,
    ks: Sequence[int] = (5, 10),
    candidate_pool: int = 30,
    exclude_tests: bool = True,
) -> list[RetrievalScore]:
    """Score one configuration at several values of K, from one retrieval pass.

    Retrieving once and slicing, rather than re-retrieving per K, because the
    reranking pass dominates the cost: with 200 examples and a 30-candidate
    pool, evaluating two values of K separately means 12,000 cross-encoder
    scorings instead of 6,000. On CPU that is the difference between a
    coffee-length wait and a long one.

    Scoring is at **file** granularity, not chunk: a chunk from the right file
    counts as a hit. The ground truth is files-that-changed, so scoring chunks
    would measure something the labels cannot support.
    """
    import time

    per_k: dict[int, dict[str, list[float]]] = {
        k: {"recall": [], "precision": [], "rr": []} for k in ks
    }
    latencies: list[float] = []

    for example in examples:
        started = time.monotonic()

        # Always retrieve the same number of *chunks*, regardless of k.
        #
        # This is not incidental. Scoring is at file granularity, and several
        # chunks routinely come from the same file — so "retrieve k chunks"
        # yields fewer than k distinct files. Sizing the fetch from k therefore
        # changes how many files each configuration even has available, and the
        # configurations stop being comparable. A fixed pool, deduplicated to
        # files afterwards, makes Recall@k mean "the top k files" for every
        # configuration. See docs/failures.md F-006.
        results = retriever.retrieve(
            example.query, limit=candidate_pool, mode=mode,
            repository=repository, exclude_tests=exclude_tests,
        )
        if reranker:
            results = reranker.rerank(example.query, results, limit=candidate_pool)
        latencies.append((time.monotonic() - started) * 1000)

        ordered_files: list[str] = []
        for scored in results:
            if scored.chunk.file_path not in ordered_files:
                ordered_files.append(scored.chunk.file_path)

        relevant = set(example.relevant_files)
        for k in ks:
            retrieved = ordered_files[:k]
            hits = relevant & set(retrieved)
            per_k[k]["recall"].append(len(hits) / len(relevant))
            per_k[k]["precision"].append(len(hits) / len(retrieved) if retrieved else 0.0)
            per_k[k]["rr"].append(
                next(
                    (1.0 / i for i, f in enumerate(retrieved, 1) if f in relevant),
                    0.0,
                )
            )

    def mean(values: list[float]) -> float:
        return round(statistics.mean(values), 4) if values else 0.0

    return [
        RetrievalScore(
            mode=mode,
            reranker=reranker.name if reranker else "none",
            k=k,
            examples=len(examples),
            recall_at_k=mean(per_k[k]["recall"]),
            precision_at_k=mean(per_k[k]["precision"]),
            mrr=mean(per_k[k]["rr"]),
            median_latency_ms=round(statistics.median(latencies), 1) if latencies else 0.0,
        )
        for k in ks
    ]


Granularity = Literal["file", "chunk"]


def format_comparison(scores: Sequence[RetrievalScore]) -> str:
    """A table comparing configurations, for docs/metrics.md."""
    if not scores:
        return "no results"
    header = (
        f"{'mode':<16} {'reranker':<18} {'Recall@' + str(scores[0].k):>9} "
        f"{'Precision@' + str(scores[0].k):>11} {'MRR':>7} {'latency ms':>9}"
    )
    lines = [header, "-" * len(header)]
    lines += [score.as_row() for score in scores]
    return "\n".join(lines)
