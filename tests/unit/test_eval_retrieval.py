"""Evaluation tests.

These test the *measurement*, which matters as much as the thing measured. A
benchmark with a bug reports numbers that are wrong in a confident, specific
way — worse than no number at all.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import pytest

from patchpilot.evaluation.retrieval import (
    build_ground_truth,
    evaluate_retrieval,
    format_comparison,
)
from patchpilot.models import Issue, IssueState
from patchpilot.rag.chunking import Chunk, ChunkKind
from patchpilot.rag.embeddings import HashingEmbeddings
from patchpilot.rag.retrieval import HybridRetriever
from patchpilot.rag.store import VectorStore
from patchpilot.tools.git import GitRepository

REPO = "acme/widget"
GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def make_issue(number: int, title: str = "Something is broken") -> Issue:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Issue(
        number=number, title=title, body="details about the failure",
        state=IssueState.CLOSED, created_at=now, updated_at=now, closed_at=now,
        html_url=f"https://github.com/acme/widget/issues/{number}",
    )


@pytest.fixture
def repo_with_history(tmp_path):
    """A repository whose commit messages reference issues, as real ones do."""
    root = tmp_path / "repo"
    root.mkdir()
    env = {**GIT_ENV, "HOME": str(tmp_path)}

    def commit(message: str, files: dict[str, str]) -> None:
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        subprocess.run(["git", "add", "."], cwd=root, env=env, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=root, env=env,
                       check=True, capture_output=True)

    subprocess.run(["git", "init", "-b", "main"], cwd=root, env=env, check=True, capture_output=True)
    commit("Initial commit", {"src/db.py": "x = 1\n", "src/render.py": "y = 2\n"})
    commit("Fix rows_where on missing tables, closes #1", {"src/db.py": "x = 2\n"})
    commit("Update docs", {"README.md": "# docs\n"})                      # no issue
    commit("Resolve template crash, fixes #2", {"src/render.py": "y = 3\n"})
    commit("Big refactor, closes #3", {f"src/mod_{i}.py": "z\n" for i in range(10)})  # too many

    return GitRepository(root)


# --- ground truth -----------------------------------------------------------


def test_ground_truth_pairs_issues_with_the_files_that_changed(repo_with_history):
    """The core trick: git already recorded the answer, so no human had to
    label anything."""
    examples = build_ground_truth([make_issue(1), make_issue(2)], repo_with_history)

    by_number = {e.issue_number: e for e in examples}
    assert by_number[1].relevant_files == ("src/db.py",)
    assert by_number[2].relevant_files == ("src/render.py",)


def test_sprawling_commits_are_excluded(repo_with_history):
    """A commit touching ten files would make recall look good for the wrong
    reason — with ten files marked correct, almost anything retrieved is a hit."""
    examples = build_ground_truth(
        [make_issue(1), make_issue(2), make_issue(3)], repo_with_history
    )

    assert 3 not in {e.issue_number for e in examples}


def test_commits_without_an_issue_reference_are_ignored(repo_with_history):
    examples = build_ground_truth([make_issue(1), make_issue(2)], repo_with_history)

    assert len(examples) == 2


def test_issues_with_no_fixing_commit_produce_no_example(repo_with_history):
    """Not every closed issue was closed by a commit — plenty are closed as
    stale or duplicate. Those are silently unusable, not an error."""
    examples = build_ground_truth([make_issue(99)], repo_with_history)

    assert examples == []


def test_the_query_is_the_issue_text(repo_with_history):
    examples = build_ground_truth([make_issue(1, "rows_where fails")], repo_with_history)

    assert "rows_where fails" in examples[0].query


# --- the metrics ------------------------------------------------------------


def build_retriever(chunks: list[Chunk]) -> HybridRetriever:
    embeddings = HashingEmbeddings(dimension=256)
    store = VectorStore(dimension=embeddings.dimension)
    store.upsert(chunks, embeddings.embed_documents([c.embedding_text() for c in chunks]))
    return HybridRetriever(store, embeddings)


def chunk_for(index: int, path: str, text: str) -> Chunk:
    return Chunk(
        id=f"{index:032x}", text=text, repository=REPO, file_path=path,
        start_line=1, end_line=5, kind=ChunkKind.FUNCTION, symbol=f"s{index}",
        language="python", is_test=False,
    )


def test_perfect_retrieval_scores_one(repo_with_history):
    """A sanity check on the scorer itself. If a retriever that always returns
    the right file does not score 1.0, the metric is broken."""
    examples = build_ground_truth([make_issue(1, "rows_where fails")], repo_with_history)
    retriever = build_retriever([chunk_for(1, "src/db.py", "rows_where fails details failure")])

    score = evaluate_retrieval(examples, retriever, repository=REPO, k=5)

    assert score.recall_at_k == 1.0
    assert score.mrr == 1.0


def test_retrieving_only_wrong_files_scores_zero(repo_with_history):
    examples = build_ground_truth([make_issue(1, "rows_where fails")], repo_with_history)
    retriever = build_retriever([chunk_for(1, "src/other.py", "rows_where fails details failure")])

    score = evaluate_retrieval(examples, retriever, repository=REPO, k=5)

    assert score.recall_at_k == 0.0
    assert score.mrr == 0.0


def test_mrr_rewards_ranking_the_right_file_higher(repo_with_history):
    """Recall only asks whether the file appeared. MRR asks how high — which
    matters when only the top few chunks fit in a prompt."""
    examples = build_ground_truth([make_issue(1, "alpha beta gamma")], repo_with_history)

    # The correct file matches weakly; three decoys match the query strongly.
    chunks = [
        chunk_for(1, "src/db.py", "alpha"),
        *[chunk_for(i, f"src/decoy_{i}.py", "alpha beta gamma delta") for i in range(2, 5)],
    ]
    score = evaluate_retrieval(examples, build_retriever(chunks), repository=REPO, k=5)

    assert score.recall_at_k == 1.0, "the file is retrieved..."
    assert score.mrr < 1.0, "...but not first, and MRR should say so"


def test_precision_falls_as_k_grows(repo_with_history):
    """Asking for more results can only add wrong ones once the right one is
    found — which is why precision and recall are reported together."""
    examples = build_ground_truth([make_issue(1, "alpha beta")], repo_with_history)
    chunks = [
        chunk_for(1, "src/db.py", "alpha beta"),
        *[chunk_for(i, f"src/other_{i}.py", "alpha beta") for i in range(2, 10)],
    ]
    retriever = build_retriever(chunks)

    at_1 = evaluate_retrieval(examples, retriever, repository=REPO, k=1)
    at_5 = evaluate_retrieval(examples, retriever, repository=REPO, k=5)

    assert at_1.precision_at_k >= at_5.precision_at_k


def test_score_records_the_configuration_that_produced_it(repo_with_history):
    """A number without the configuration behind it cannot be compared to
    anything."""
    examples = build_ground_truth([make_issue(1)], repo_with_history)
    retriever = build_retriever([chunk_for(1, "src/db.py", "details failure")])

    score = evaluate_retrieval(examples, retriever, repository=REPO, mode="sparse", k=3)

    assert score.mode == "sparse"
    assert score.reranker == "none"
    assert score.k == 3
    assert score.examples == 1


def test_evaluating_no_examples_does_not_divide_by_zero():
    score = evaluate_retrieval([], build_retriever([]), repository=REPO, k=5)

    assert score.recall_at_k == 0.0
    assert score.examples == 0


def test_comparison_table_renders(repo_with_history):
    examples = build_ground_truth([make_issue(1)], repo_with_history)
    retriever = build_retriever([chunk_for(1, "src/db.py", "details failure")])
    scores = [
        evaluate_retrieval(examples, retriever, repository=REPO, mode=mode, k=5)
        for mode in ("dense", "sparse", "hybrid")
    ]

    table = format_comparison(scores)

    assert "Recall@5" in table
    assert "hybrid" in table


def test_test_files_are_excluded_from_the_labels(repo_with_history):
    """Regression guard for a measurement bug (docs/failures.md F-005).

    A fix commit usually touches the source file *and* its test. Retrieval is
    configured with `exclude_tests=True`, so a test file in the ground truth is
    a target that can never be hit — capping recall below 1.0 by construction,
    and by a different amount for every example.
    """
    import subprocess

    root = repo_with_history.path
    env = {**GIT_ENV, "HOME": str(root.parent)}
    (root / "src" / "thing.py").write_text("a = 1\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_thing.py").write_text("def test_a(): pass\n")
    subprocess.run(["git", "add", "."], cwd=root, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Fix thing, closes #7"], cwd=root, env=env,
                   check=True, capture_output=True)

    examples = build_ground_truth([make_issue(7)], GitRepository(root))

    assert examples[0].relevant_files == ("src/thing.py",)


def test_keeping_test_files_is_available_for_comparison(repo_with_history):
    """The exclusion is a parameter, not a hard-coded assumption — so the effect
    of the choice can itself be measured."""
    import subprocess

    root = repo_with_history.path
    env = {**GIT_ENV, "HOME": str(root.parent)}
    (root / "src" / "thing.py").write_text("a = 2\n")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_thing.py").write_text("def test_b(): pass\n")
    subprocess.run(["git", "add", "."], cwd=root, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Fix thing again, closes #8"], cwd=root, env=env,
                   check=True, capture_output=True)

    examples = build_ground_truth([make_issue(8)], GitRepository(root), exclude_test_files=False)

    assert set(examples[0].relevant_files) == {"src/thing.py", "tests/test_thing.py"}


def test_multiple_k_values_come_from_one_retrieval_pass(repo_with_history):
    """Reranking dominates the benchmark's cost, so K values are scored by
    slicing one ranked list rather than retrieving again."""
    from patchpilot.evaluation.retrieval import evaluate_at_multiple_k

    examples = build_ground_truth([make_issue(1, "alpha beta")], repo_with_history)
    chunks = [
        chunk_for(1, "src/db.py", "alpha beta"),
        *[chunk_for(i, f"src/other_{i}.py", "alpha beta") for i in range(2, 12)],
    ]
    retriever = build_retriever(chunks)

    scores = evaluate_at_multiple_k(examples, retriever, repository=REPO, ks=(1, 5, 10))

    assert [s.k for s in scores] == [1, 5, 10]
    # Recall cannot fall as K grows — more results can only add hits.
    assert scores[0].recall_at_k <= scores[1].recall_at_k <= scores[2].recall_at_k


def test_the_candidate_pool_does_not_depend_on_k(repo_with_history):
    """Regression guard for a comparability bug (docs/failures.md F-006).

    Scoring is at file granularity and several chunks routinely come from the
    same file, so "retrieve k chunks" yields fewer than k distinct files.
    Sizing the fetch from k changes how many files a configuration even has
    available, which made configurations evaluated at different k values
    incomparable. Every configuration must now see the same pool.
    """
    from patchpilot.evaluation.retrieval import evaluate_at_multiple_k

    requested: list[int] = []

    class RecordingRetriever:
        def __init__(self, inner):
            self._inner = inner

        def retrieve(self, query, *, limit, **kwargs):
            requested.append(limit)
            return self._inner.retrieve(query, limit=limit, **kwargs)

    examples = build_ground_truth([make_issue(1, "alpha beta")], repo_with_history)
    inner = build_retriever([chunk_for(i, f"src/f{i}.py", "alpha beta") for i in range(1, 8)])

    evaluate_at_multiple_k(
        examples, RecordingRetriever(inner), repository=REPO, ks=(1, 5, 10), candidate_pool=25
    )

    assert requested == [25], "the fetch size must come from the pool, not from k"
