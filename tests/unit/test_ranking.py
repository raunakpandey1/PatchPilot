"""Issue ranking tests.

The ranker is deterministic arithmetic, so these tests assert exact behaviour:
which issues are blocked outright, which factors fire, and that the same input
always produces the same order.

That last property is why the ranker is not an LLM. If the ordering wobbled
between runs, no later measurement would be comparable — you could never tell
whether retrieval improved or the ranker simply felt different that day.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from patchpilot.agent.ranking import rank_issue, rank_issues
from patchpilot.models import Issue, IssueState, Label

NOW = datetime(2026, 9, 17, tzinfo=UTC)

GOOD_BODY = """
When I call `db["missing"].rows_where()` it returns an empty list instead of raising.

Steps to reproduce:

```python
import sqlite_utils
db = sqlite_utils.Database(memory=True)
list(db["nope"].rows_where("x = 1"))   # expected OperationalError
```

Expected behaviour: an error. Actual behaviour: silently empty.
"""


def make_issue(
    number: int = 1,
    title: str = "rows_where() silently returns empty for missing tables",
    body: str | None = GOOD_BODY,
    labels: tuple[str, ...] = ("bug",),
    assignees: tuple[str, ...] = (),
    comments: int = 2,
    updated_days_ago: int = 10,
    state: IssueState = IssueState.OPEN,
    is_pr: bool = False,
) -> Issue:
    updated = NOW - timedelta(days=updated_days_ago)
    return Issue(
        number=number,
        title=title,
        body=body,
        state=state,
        labels=tuple(Label(name=n) for n in labels),
        assignees=assignees,
        author="someone",
        comments_count=comments,
        created_at=updated - timedelta(days=1),
        updated_at=updated,
        html_url=f"https://github.com/o/r/issues/{number}",
        is_pull_request=is_pr,
    )


# --- hard blockers ----------------------------------------------------------


def test_a_well_formed_bug_is_attemptable():
    ranked = rank_issue(make_issue(), now=NOW)

    assert ranked.verdict == "attempt"
    assert ranked.score > 0.65
    assert not ranked.blockers


def test_assigned_issues_are_blocked_outright():
    """Someone is already on it. Opening a competing PR wastes a maintainer's
    time — a social constraint enforced in code, not left to a prompt."""
    ranked = rank_issue(make_issue(assignees=("maintainer",)), now=NOW)

    assert ranked.verdict == "skip"
    assert ranked.score == 0.0
    assert any("assigned" in b for b in ranked.blockers)


def test_pull_requests_are_blocked():
    ranked = rank_issue(make_issue(is_pr=True), now=NOW)

    assert ranked.verdict == "skip"
    assert any("pull request" in b for b in ranked.blockers)


@pytest.mark.parametrize("label", ["wontfix", "duplicate", "question", "needs-info"])
def test_blocking_labels_are_blocked(label):
    ranked = rank_issue(make_issue(labels=(label,)), now=NOW)

    assert ranked.verdict == "skip"
    assert any(label in b for b in ranked.blockers)


@pytest.mark.parametrize("body", [None, "", "   ", "please fix"])
def test_issues_without_a_usable_body_are_blocked(body):
    ranked = rank_issue(make_issue(body=body), now=NOW)

    assert ranked.verdict == "skip"
    assert any("short" in b or "empty" in b for b in ranked.blockers)


def test_blockers_short_circuit_scoring():
    """A blocker is a gate, not a penalty a good score could outweigh.

    An otherwise perfect issue that is already assigned scores exactly zero —
    not 'high, minus a bit'.
    """
    perfect_but_assigned = make_issue(labels=("good first issue", "bug"), assignees=("dev",))
    ranked = rank_issue(perfect_but_assigned, now=NOW)

    assert ranked.score == 0.0
    assert ranked.factors == ()


# --- factors ----------------------------------------------------------------


def test_reproduction_signals_raise_the_score():
    with_repro = rank_issue(make_issue(), now=NOW)
    without = rank_issue(
        make_issue(body="Something is wrong with the rows_where function, please look into it."),
        now=NOW,
    )

    assert with_repro.score > without.score
    repro_factor = next(f for f in with_repro.factors if f.name == "reproducibility")
    assert repro_factor.score > 0


def test_reproducibility_is_the_heaviest_factor():
    """A bug we cannot reproduce is a bug we cannot verify a fix for, and an
    unverifiable patch is worse than none — it looks like progress."""
    factors = {f.name: f.weight for f in rank_issue(make_issue(), now=NOW).factors}

    assert factors["reproducibility"] == max(factors.values())


def test_inviting_labels_help_and_large_labels_hurt():
    inviting = rank_issue(make_issue(labels=("good first issue",)), now=NOW)
    large = rank_issue(make_issue(labels=("epic",)), now=NOW)

    assert inviting.score > large.score


def test_many_comments_count_against_an_issue():
    """A few comments mean confirmation. Many usually mean disagreement about
    what the right fix is, which an agent cannot settle."""
    quiet = rank_issue(make_issue(comments=2), now=NOW)
    contested = rank_issue(make_issue(comments=40), now=NOW)

    assert quiet.score > contested.score


def test_stale_issues_score_lower():
    fresh = rank_issue(make_issue(updated_days_ago=5), now=NOW)
    ancient = rank_issue(make_issue(updated_days_ago=800), now=NOW)

    assert fresh.score > ancient.score


def test_vague_titles_score_lower():
    specific = rank_issue(make_issue(title="rows_where() fails on missing tables"), now=NOW)
    vague = rank_issue(make_issue(title="bug"), now=NOW)

    assert specific.score > vague.score


# --- aggregation and explainability -----------------------------------------


def test_ranking_is_deterministic():
    """Byte-identical results across runs. This is the property that makes any
    later improvement measurable."""
    issues = [make_issue(number=n, comments=n) for n in range(1, 12)]

    first = [(r.issue.number, round(r.score, 6)) for r in rank_issues(issues, now=NOW)]
    second = [(r.issue.number, round(r.score, 6)) for r in rank_issues(issues, now=NOW)]

    assert first == second


def test_ties_break_on_issue_number():
    """With equal scores the order must still be fully determined by the input."""
    identical = [make_issue(number=n) for n in (7, 3, 9)]

    ordered = rank_issues(identical, now=NOW)

    assert [r.issue.number for r in ordered] == [3, 7, 9]


def test_results_are_sorted_best_first():
    issues = [
        make_issue(number=1, labels=("epic",), body="vague request", comments=50),
        make_issue(number=2, labels=("good first issue", "bug")),
        make_issue(number=3, labels=()),
    ]

    ranked = rank_issues(issues, now=NOW)

    assert ranked[0].issue.number == 2
    assert [r.score for r in ranked] == sorted((r.score for r in ranked), reverse=True)


def test_limit_truncates_after_sorting():
    issues = [make_issue(number=n) for n in range(1, 20)]

    assert len(rank_issues(issues, now=NOW, limit=5)) == 5


def test_score_is_always_a_probability_like_number():
    for issue in [
        make_issue(),
        make_issue(labels=("epic",), comments=99, updated_days_ago=3000),
        make_issue(labels=("good first issue", "bug", "help wanted")),
    ]:
        assert 0.0 <= rank_issue(issue, now=NOW).score <= 1.0


def test_explanation_shows_the_full_derivation():
    """An agent that cannot say why it chose something is an agent nobody will
    let near their repository."""
    explanation = rank_issue(make_issue(), now=NOW).explain()

    assert "verdict:" in explanation
    assert "reproducibility" in explanation
    assert "labels" in explanation
    # Every factor that contributed appears with its contribution.
    for factor in rank_issue(make_issue(), now=NOW).factors:
        assert factor.name in explanation


def test_explanation_lists_blockers():
    explanation = rank_issue(make_issue(assignees=("dev",)), now=NOW).explain()

    assert "blocked by" in explanation
    assert "dev" in explanation
