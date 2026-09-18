"""Deciding which issues are worth attempting — deterministically.

Why not just ask the model
--------------------------
It would work. It would also be, in order of increasing seriousness:

* **Expensive.** Ranking 200 issues means 200 judgements, or one very long
  prompt. On a free tier that is quota spent before any work is done.
* **Unstable.** Run it twice, get two orderings. Benchmark results stop being
  comparable across runs, which makes measuring any *other* improvement
  impossible — you can never tell whether retrieval got better or the ranker
  just felt different today.
* **Unexplainable.** "Why was issue #412 skipped?" is answerable here by
  printing the factor table. With an LLM the answer is a second LLM call asking
  it to justify itself, which is not the same thing as the reason it actually
  used.

So ranking is arithmetic over observable properties, and every score comes with
its own derivation.

What this is *not*
------------------
It does not judge whether a fix is correct, or whether the issue is a good idea.
Those are judgement, and judgement is what the model is for. This answers a
narrower, mechanical question: **is this issue shaped like something an
automated agent could attempt and verify?**

The distinction — lookup in code, judgement in the model — is the same one
applied in :mod:`patchpilot.analysis.repository`.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from patchpilot.models import Issue

Verdict = Literal["attempt", "maybe", "skip"]

# Labels that say "do not work on this" more reliably than any heuristic.
BLOCKING_LABELS = frozenset(
    {"wontfix", "won't fix", "duplicate", "invalid", "question", "discussion",
     "stale", "needs-info", "needs more info", "blocked", "on hold"}
)

# Labels maintainers use to mean "this is available and scoped".
INVITING_LABELS = frozenset(
    {"good first issue", "good-first-issue", "help wanted", "help-wanted",
     "bug", "documentation", "docs", "easy", "beginner"}
)

# Labels that usually mean "large, open-ended, or design work".
LARGE_LABELS = frozenset(
    {"epic", "enhancement", "feature", "feature request", "proposal", "rfc",
     "research", "design", "breaking change", "refactor"}
)

TRACEBACK_MARKERS = ("Traceback (most recent call last)", "File \"", "Error:", "Exception:")
REPRODUCTION_MARKERS = ("steps to reproduce", "to reproduce", "reproduction",
                        "expected behaviour", "expected behavior", "actual behaviour",
                        "actual behavior", "how to reproduce")
CODE_FENCE = re.compile(r"```")


class Factor(BaseModel):
    """One observable property and what it contributed to the score."""

    model_config = ConfigDict(frozen=True)

    name: str
    score: float = Field(ge=-1.0, le=1.0)  # normalised judgement of this property
    weight: float = Field(ge=0.0)          # how much this property matters
    reason: str                            # human-readable, shown in explanations

    @property
    def contribution(self) -> float:
        return self.score * self.weight


class RankedIssue(BaseModel):
    """An issue with a score and the complete derivation of that score."""

    model_config = ConfigDict(frozen=True)

    issue: Issue
    score: float = Field(ge=0.0, le=1.0)
    verdict: Verdict
    factors: tuple[Factor, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def is_actionable(self) -> bool:
        return self.verdict == "attempt"

    def explain(self) -> str:
        """The full derivation, for a CLI, a log, or an approval screen.

        An agent that cannot say why it chose something is an agent nobody will
        let near their repository.
        """
        lines = [
            f"#{self.issue.number} — {self.issue.title}",
            f"  verdict: {self.verdict.upper()}   score: {self.score:.2f}",
        ]
        if self.blockers:
            lines.append("  blocked by:")
            lines += [f"    ✗ {b}" for b in self.blockers]
        if self.factors:
            lines.append("  factors:")
            for f in sorted(self.factors, key=lambda f: -abs(f.contribution)):
                sign = "+" if f.contribution >= 0 else "-"
                lines.append(
                    f"    {sign} {f.name:<16} {abs(f.contribution):>5.2f}  {f.reason}"
                )
        return "\n".join(lines)


def rank_issue(issue: Issue, *, now: datetime | None = None) -> RankedIssue:
    """Score one issue on how attemptable it looks."""
    now = now or datetime.now(UTC)
    blockers = _find_blockers(issue)

    if blockers:
        # Hard blockers short-circuit. There is no score worth computing for an
        # issue somebody is already assigned to — this is a gate, not a penalty
        # that a high score elsewhere could outweigh.
        return RankedIssue(issue=issue, score=0.0, verdict="skip", blockers=tuple(blockers))

    factors = (
        _label_factor(issue),
        _reproducibility_factor(issue),
        _scope_factor(issue),
        _clarity_factor(issue),
        _discussion_factor(issue),
        _staleness_factor(issue, now),
    )

    total_weight = sum(f.weight for f in factors)
    raw = sum(f.contribution for f in factors) / total_weight if total_weight else 0.0
    # Factor scores run -1..1; map to 0..1 so the number reads as a probability-ish
    # confidence rather than a signed quantity.
    score = max(0.0, min(1.0, (raw + 1.0) / 2.0))

    return RankedIssue(
        issue=issue, score=score, verdict=_verdict_for(score), factors=factors
    )


def rank_issues(
    issues: list[Issue], *, now: datetime | None = None, limit: int | None = None
) -> list[RankedIssue]:
    """Rank many issues, best first.

    Ties break on issue number so the ordering is completely determined by the
    input. Two runs over the same data produce byte-identical results, which is
    what makes any later improvement measurable.
    """
    ranked = [rank_issue(issue, now=now) for issue in issues]
    ranked.sort(key=lambda r: (-r.score, r.issue.number))
    return ranked[:limit] if limit else ranked


# --- blockers ---------------------------------------------------------------


def _find_blockers(issue: Issue) -> list[str]:
    """Reasons to refuse outright, independent of any score."""
    blockers: list[str] = []

    if issue.is_pull_request:
        blockers.append("this is a pull request, not an issue")
    if issue.state != "open":
        blockers.append(f"issue is {issue.state}")
    if issue.is_assigned:
        # Someone is already working on it. Opening a competing PR is rude and
        # wastes a maintainer's time — a social constraint, enforced in code.
        blockers.append(f"already assigned to {', '.join(issue.assignees)}")

    if blocking := issue.label_names & BLOCKING_LABELS:
        blockers.append(f"labelled {', '.join(sorted(blocking))}")

    body = (issue.body or "").strip()
    if len(body) < 30:
        blockers.append("body is empty or too short to act on")

    return blockers


# --- factors ----------------------------------------------------------------


def _label_factor(issue: Issue) -> Factor:
    labels = issue.label_names
    if inviting := labels & INVITING_LABELS:
        return Factor(
            name="labels", score=1.0, weight=2.0,
            reason=f"labelled {', '.join(sorted(inviting))}",
        )
    if large := labels & LARGE_LABELS:
        return Factor(
            name="labels", score=-0.6, weight=2.0,
            reason=f"labelled {', '.join(sorted(large))} — likely open-ended",
        )
    return Factor(name="labels", score=0.0, weight=2.0, reason="no informative labels")


def _reproducibility_factor(issue: Issue) -> Factor:
    """Can we tell whether we fixed it?

    Weighted highest of all factors. A bug we cannot reproduce is a bug we
    cannot verify a fix for, and an unverifiable patch is worse than no patch —
    it looks like progress.
    """
    text = issue.text
    lower = text.lower()

    has_traceback = any(marker in text for marker in TRACEBACK_MARKERS)
    has_code = len(CODE_FENCE.findall(text)) >= 2
    has_steps = any(marker in lower for marker in REPRODUCTION_MARKERS)

    signals = sum([has_traceback, has_code, has_steps])
    if signals >= 2:
        return Factor(
            name="reproducibility", score=1.0, weight=3.0,
            reason=f"{signals} reproduction signals (traceback/code/steps)",
        )
    if signals == 1:
        return Factor(
            name="reproducibility", score=0.2, weight=3.0,
            reason="one reproduction signal — may be reproducible",
        )
    return Factor(
        name="reproducibility", score=-0.8, weight=3.0,
        reason="no traceback, code sample or reproduction steps",
    )


def _scope_factor(issue: Issue) -> Factor:
    """Rough proxy for how much work this is.

    Body length is a crude signal and is treated as such — a low weight, and a
    band rather than a slope.
    """
    length = len(issue.text)
    if length > 4000:
        return Factor(
            name="scope", score=-0.7, weight=1.5,
            reason=f"very long description ({length} chars) — likely a large change",
        )
    if length > 1500:
        return Factor(name="scope", score=-0.2, weight=1.5, reason="long description")
    if length < 200:
        return Factor(
            name="scope", score=-0.3, weight=1.5,
            reason="very short description — may be under-specified",
        )
    return Factor(name="scope", score=0.8, weight=1.5, reason="focused description")


def _clarity_factor(issue: Issue) -> Factor:
    title = issue.title.strip()
    vague = {"bug", "help", "question", "issue", "problem", "error", "doesn't work"}
    if title.lower() in vague or len(title) < 15:
        return Factor(name="clarity", score=-0.6, weight=1.0, reason="vague title")
    return Factor(name="clarity", score=0.5, weight=1.0, reason="specific title")


def _discussion_factor(issue: Issue) -> Factor:
    """Comments are evidence, up to a point.

    A few comments usually mean a maintainer confirmed the bug. Many comments
    usually mean disagreement about what the right fix even is — which an agent
    is in no position to settle.
    """
    n = issue.comments_count
    if n == 0:
        return Factor(
            name="discussion", score=-0.2, weight=1.0,
            reason="no comments — unconfirmed by a maintainer",
        )
    if n <= 5:
        return Factor(
            name="discussion", score=0.6, weight=1.0, reason=f"{n} comments — some confirmation"
        )
    return Factor(
        name="discussion", score=-0.5, weight=1.0,
        reason=f"{n} comments — likely contested or complex",
    )


def _staleness_factor(issue: Issue, now: datetime) -> Factor:
    days = (now - issue.updated_at).days
    if days > 365:
        return Factor(
            name="staleness", score=-0.7, weight=1.0,
            reason=f"untouched for {days} days — may no longer apply",
        )
    if days > 90:
        return Factor(name="staleness", score=-0.2, weight=1.0, reason=f"quiet for {days} days")
    return Factor(name="staleness", score=0.5, weight=1.0, reason=f"active ({days} days ago)")


def _verdict_for(score: float) -> Verdict:
    """Thresholds, stated once.

    These are a starting point, not a result. Phase 11 measures how well they
    predict success and they will be tuned against that evidence — at which
    point the change is one line here, and its effect is measurable because the
    ranker is deterministic.
    """
    if score >= 0.65:
        return "attempt"
    if score >= 0.45:
        return "maybe"
    return "skip"
