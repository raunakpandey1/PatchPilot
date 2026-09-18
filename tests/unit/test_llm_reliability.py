"""Fallback and caching — the two mechanisms that make a free tier workable.

Both were added in response to real events: a 503 that persisted across retries
on the first live run, and a hard daily call limit. These tests pin the
behaviour that makes them safe rather than merely present.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from patchpilot.llm.base import (
    LLMProvider,
    LLMResponseInvalid,
    LLMUnavailable,
    user,
)
from patchpilot.llm.cache import BudgetExceeded, CachingProvider, CallBudget
from patchpilot.llm.fake import FakeProvider
from patchpilot.llm.fallback import FallbackProvider


class Verdict(BaseModel):
    ok: bool
    why: str


class AlwaysFails:
    """A provider that is always unavailable."""

    def __init__(self, name: str = "broken/model") -> None:
        self._name = name
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    def complete(self, messages, **kwargs):
        self.calls += 1
        raise LLMUnavailable(f"{self._name} returned 503")

    def complete_structured(self, messages, schema, **kwargs):
        self.calls += 1
        raise LLMUnavailable(f"{self._name} returned 503")


class RejectsRequest:
    """A provider that rejects the request itself — a 400, or a schema mismatch."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "strict/model"

    def complete(self, messages, **kwargs):
        self.calls += 1
        raise LLMResponseInvalid("malformed request")

    def complete_structured(self, messages, schema, **kwargs):
        self.calls += 1
        raise LLMResponseInvalid("response did not match schema")


# --- fallback ---------------------------------------------------------------


def test_falls_back_when_the_primary_is_unavailable():
    """The situation that prompted this: the primary 503'd for minutes while
    other models served normally. Retrying harder cannot fix that."""
    primary = AlwaysFails("gemini/primary")
    secondary = FakeProvider(responses=["answer"], model_name="gemini/secondary")

    provider = FallbackProvider([primary, secondary])
    result = provider.complete([user("hi")])

    assert result.text == "answer"
    assert primary.calls == 1
    assert provider.fallback_count == 1


def test_a_bad_request_does_not_trigger_fallback():
    """The distinction that makes fallback safe rather than wasteful.

    A 400 or a schema violation will fail identically at every model. Falling
    back would spend three models' quota collecting three copies of the same
    failure — and would hide the real bug behind a slow one.
    """
    strict = RejectsRequest()
    spare = FakeProvider(responses=["should never be reached"])

    provider = FallbackProvider([strict, spare])

    with pytest.raises(LLMResponseInvalid):
        provider.complete([user("hi")])

    assert strict.calls == 1
    assert spare.call_count == 0, "a bad request must not be retried elsewhere"


def test_the_primary_is_used_when_it_works():
    primary = FakeProvider(responses=["from primary"], model_name="a")
    secondary = FakeProvider(responses=["from secondary"], model_name="b")

    provider = FallbackProvider([primary, secondary])

    assert provider.complete([user("hi")]).text == "from primary"
    assert provider.fallback_count == 0
    assert secondary.call_count == 0


def test_name_reports_which_model_actually_answered():
    """A benchmark row produced by a fallback is not comparable with one from
    the primary, so results must record the model that produced them."""
    provider = FallbackProvider(
        [AlwaysFails("gemini/primary"), FakeProvider(responses=["x"], model_name="gemini/backup")]
    )

    provider.complete([user("hi")])

    assert provider.name == "gemini/backup"
    assert provider.chain == ("gemini/primary", "gemini/backup")


def test_exhausting_the_chain_raises_with_every_model_named():
    provider = FallbackProvider([AlwaysFails("a"), AlwaysFails("b")])

    with pytest.raises(LLMUnavailable, match="every provider in the chain"):
        provider.complete([user("hi")])


def test_fallback_works_for_structured_output_too():
    provider = FallbackProvider(
        [AlwaysFails(), FakeProvider(responses=[Verdict(ok=True, why="fine")])]
    )

    assert provider.complete_structured([user("hi")], Verdict).value.ok is True


def test_an_empty_chain_is_rejected_at_construction():
    with pytest.raises(ValueError):
        FallbackProvider([])


def test_fallback_satisfies_the_provider_protocol():
    assert isinstance(FallbackProvider([FakeProvider()]), LLMProvider)


# --- caching ----------------------------------------------------------------


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    return tmp_path / "llm_cache"


def test_a_repeated_prompt_costs_nothing(cache_dir):
    """The mechanism that makes iterating on a fixed pipeline affordable."""
    inner = FakeProvider(responses=["answer one", "answer two"])
    provider = CachingProvider(inner, cache_dir)

    first = provider.complete([user("same question")])
    second = provider.complete([user("same question")])

    assert first.text == second.text == "answer one"
    assert inner.call_count == 1, "the second call must not reach the provider"
    assert provider.stats["cache_hits"] == 1


def test_a_different_prompt_is_a_different_entry(cache_dir):
    inner = FakeProvider(responses=["one", "two"])
    provider = CachingProvider(inner, cache_dir)

    assert provider.complete([user("question A")]).text == "one"
    assert provider.complete([user("question B")]).text == "two"
    assert inner.call_count == 2


@pytest.mark.parametrize(
    "changed",
    [
        {"system": "different system prompt"},
        {"temperature": 0.7},
        {"max_output_tokens": 999},
    ],
)
def test_every_input_that_can_change_the_answer_changes_the_key(cache_dir, changed):
    """Leave any of these out of the key and the cache serves an answer to a
    different question — worse than no cache, because it is invisible."""
    inner = FakeProvider(responses=["first", "second"])
    provider = CachingProvider(inner, cache_dir)

    provider.complete([user("q")])
    provider.complete([user("q")], **changed)

    assert inner.call_count == 2


def test_structured_responses_round_trip_through_the_cache(cache_dir):
    inner = FakeProvider(responses=[Verdict(ok=True, why="because")])
    provider = CachingProvider(inner, cache_dir)

    first = provider.complete_structured([user("q")], Verdict)
    second = provider.complete_structured([user("q")], Verdict)

    assert first.value == second.value
    assert isinstance(second.value, Verdict)
    assert inner.call_count == 1


def test_the_cache_survives_a_new_process(cache_dir):
    """A crashed run must not have to repay for answers it already obtained."""
    CachingProvider(FakeProvider(responses=["persisted"]), cache_dir).complete([user("q")])

    inner = FakeProvider(responses=["should not be used"])
    revived = CachingProvider(inner, cache_dir)

    assert revived.complete([user("q")]).text == "persisted"
    assert inner.call_count == 0


def test_a_corrupt_entry_is_treated_as_a_miss(cache_dir):
    """A bad cache file must never break a run."""
    provider = CachingProvider(FakeProvider(responses=["fresh"]), cache_dir)
    provider.complete([user("q")])

    for path in cache_dir.glob("*.json"):
        path.write_text("{ not json")

    assert provider.complete([user("q")]).text == "fresh"


def test_caching_can_be_disabled(cache_dir):
    inner = FakeProvider(responses=["a", "b"])
    provider = CachingProvider(inner, cache_dir, enabled=False)

    provider.complete([user("q")])
    provider.complete([user("q")])

    assert inner.call_count == 2


# --- budget -----------------------------------------------------------------


def test_the_budget_stops_a_runaway_loop(cache_dir):
    """Phase 7's debug loop calls the model until tests pass or a bound is hit.
    A bug in that bound could spend a day's quota in minutes."""
    provider = CachingProvider(
        FakeProvider(responses=["x"]), cache_dir, budget=CallBudget(max_calls=2)
    )

    provider.complete([user("q1")])
    provider.complete([user("q2")])

    with pytest.raises(BudgetExceeded, match="budget exhausted"):
        provider.complete([user("q3")])


def test_cache_hits_do_not_consume_budget(cache_dir):
    """The whole point of pairing the two: re-running a cached pipeline is free
    in both quota and budget."""
    provider = CachingProvider(
        FakeProvider(responses=["x"]), cache_dir, budget=CallBudget(max_calls=1)
    )

    provider.complete([user("q")])
    for _ in range(10):
        provider.complete([user("q")])  # all cache hits

    assert provider.budget.calls_made == 1
    assert provider.stats["cache_hits"] == 10


def test_budget_tracks_tokens_as_well_as_calls(cache_dir):
    provider = CachingProvider(
        FakeProvider(responses=["a", "b"], input_tokens=100, output_tokens=50), cache_dir
    )

    provider.complete([user("q1")])
    provider.complete([user("q2")])

    assert provider.budget.tokens_used == 300


def test_an_unlimited_budget_never_raises(cache_dir):
    provider = CachingProvider(FakeProvider(responses=["x"]), cache_dir, budget=CallBudget(None))

    for i in range(20):
        provider.complete([user(f"q{i}")])

    assert provider.budget.remaining == float("inf")


def test_caching_provider_satisfies_the_protocol(cache_dir):
    assert isinstance(CachingProvider(FakeProvider(), cache_dir), LLMProvider)


def test_the_whole_stack_composes(cache_dir):
    """Cache outermost, fallback inside — so a cache hit neither consumes budget
    nor cares which model in the chain originally answered."""
    primary = AlwaysFails()
    stack = CachingProvider(
        FallbackProvider([primary, FakeProvider(responses=["answered"])]),
        cache_dir,
        budget=CallBudget(max_calls=5),
    )

    assert stack.complete([user("q")]).text == "answered"
    assert stack.complete([user("q")]).text == "answered"
    assert primary.calls == 1, "the second call never reached the chain at all"
    assert stack.budget.calls_made == 1


def test_the_cache_key_does_not_move_when_the_chain_falls_back(cache_dir):
    """Regression guard for a silent cache bug (docs/failures.md F-007).

    `FallbackProvider.name` reports whichever model last answered, so it changes
    after a fallback. The cache key originally included it, which meant the same
    request hashed differently on the next call and never hit — while every test
    of the cache in isolation still passed, because a plain provider's name
    never changes.
    """
    primary = AlwaysFails()
    chain = FallbackProvider([primary, FakeProvider(responses=["answered"])])
    provider = CachingProvider(chain, cache_dir)

    provider.complete([user("q")])
    name_after_fallback = chain.name
    provider.complete([user("q")])

    assert name_after_fallback != "broken/model", "the chain's name did move..."
    assert primary.calls == 1, "...but the cache key must not have moved with it"
    assert provider.stats["cache_hits"] == 1
