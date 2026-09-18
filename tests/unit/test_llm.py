"""LLM abstraction tests.

No network, no API key, no cost. The point of the abstraction is that the rest
of the system cannot tell a scripted provider from a real one — these tests are
where that claim is checked.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from patchpilot.config import Settings
from patchpilot.llm import build_provider
from patchpilot.llm.base import (
    Completion,
    LLMProvider,
    LLMResponseInvalid,
    LLMUnavailable,
    Usage,
    assistant,
    user,
)
from patchpilot.llm.fake import FakeProvider


class RootCause(BaseModel):
    file: str
    line: int
    explanation: str


# --- the contract -----------------------------------------------------------


def test_fake_provider_satisfies_the_protocol():
    """If this fails, tests are exercising a different interface from production."""
    assert isinstance(FakeProvider(), LLMProvider)


def test_gemini_provider_satisfies_the_protocol():
    from patchpilot.llm.gemini import GeminiProvider

    provider = GeminiProvider(api_key="unused", client=object())

    assert isinstance(provider, LLMProvider)
    assert provider.name.startswith("gemini/")


# --- usage accounting -------------------------------------------------------


def test_usage_is_returned_with_every_completion():
    """Phase 10 reports cost per node; no node should have to remember to measure."""
    provider = FakeProvider(responses=["hello"], input_tokens=120, output_tokens=30)

    completion = provider.complete([user("hi")])

    assert completion.usage.input_tokens == 120
    assert completion.usage.total_tokens == 150


def test_usage_adds_up():
    assert (Usage(10, 5, 1.0) + Usage(3, 2, 0.5)).total_tokens == 20


def test_truncation_is_detectable():
    """A truncated response is still valid text, so it fails silently unless
    checked. A half-written patch that parses is worse than one that errors."""
    assert Completion(text="half a diff", model="m", finish_reason="MAX_TOKENS").was_truncated
    assert not Completion(text="complete", model="m", finish_reason="stop").was_truncated


# --- structured output ------------------------------------------------------


def test_structured_output_returns_a_validated_object():
    """A node receiving a typed object either works or raises. A node parsing
    prose breaks quietly whenever the model rewords its answer."""
    expected = RootCause(file="db.py", line=42, explanation="missing table check")
    provider = FakeProvider(responses=[expected])

    result = provider.complete_structured([user("what broke?")], RootCause)

    assert result.value.file == "db.py"
    assert result.value.line == 42


def test_structured_output_rejects_a_mismatched_response():
    """The failure mode worth testing: the model replied, but not with what was
    asked for. Retrying identical input rarely helps — the prompt is wrong."""
    provider = FakeProvider(responses=['{"file": "db.py"}'])  # missing fields

    with pytest.raises(LLMResponseInvalid):
        provider.complete_structured([user("what broke?")], RootCause)


def test_provider_failures_propagate():
    provider = FakeProvider(responses=[LLMUnavailable("service down")])

    with pytest.raises(LLMUnavailable):
        provider.complete([user("hi")])


# --- the fake's own affordances ---------------------------------------------


def test_the_prompt_is_inspectable():
    """Lets a test assert that retrieved context actually reached the model —
    the difference between 'RAG ran' and 'RAG mattered'."""
    provider = FakeProvider(responses=["ok"])

    provider.complete([user("here is sqlite_utils/db.py")], system="you are a debugger")

    assert "sqlite_utils/db.py" in provider.last_call.prompt_text
    assert "you are a debugger" in provider.last_call.prompt_text
    assert provider.call_count == 1


def test_responses_are_consumed_in_order():
    provider = FakeProvider(responses=["first", "second"])

    assert provider.complete([user("a")]).text == "first"
    assert provider.complete([user("b")]).text == "second"


def test_the_last_response_repeats_forever():
    """Simulates a model that will not stop saying the same wrong thing — which
    is exactly what the Phase 7 debug loop has to survive."""
    provider = FakeProvider(responses=["still wrong"])

    assert [provider.complete([user("try again")]).text for _ in range(5)] == ["still wrong"] * 5


def test_conversation_history_is_passed_through():
    provider = FakeProvider(responses=["ok"])

    provider.complete([user("first"), assistant("reply"), user("second")])

    assert len(provider.last_call.messages) == 3


# --- the factory ------------------------------------------------------------


def test_factory_builds_the_configured_provider():
    provider = build_provider(Settings(_env_file=None, llm_provider="fake"))

    assert isinstance(provider, FakeProvider)


def test_factory_gives_an_actionable_error_without_a_key():
    """An error message that tells you what to do is worth more than a stack trace."""
    with pytest.raises(LLMUnavailable, match="aistudio.google.com"):
        build_provider(Settings(_env_file=None, llm_provider="gemini", gemini_api_key=None))
