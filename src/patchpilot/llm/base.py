"""The LLM provider interface.

Why an abstraction at all
-------------------------
Not because abstraction is virtuous. Because of a concrete constraint: this
project is built on a free tier, and the model behind it will change — a free
Gemini key today, possibly a stronger paid model for one final benchmark run,
and a scripted fake in every test.

Three implementations, one interface:

* :class:`~patchpilot.llm.gemini.GeminiProvider` — the real thing.
* :class:`~patchpilot.llm.fake.FakeProvider` — scripted, deterministic, free.
  Every unit test uses it.
* (later) any other provider, added without touching a single agent node.

What the interface deliberately does *not* expose
-------------------------------------------------
No provider-specific knobs. No ``safety_settings``, no ``top_k``, no
Gemini-shaped config objects. The moment a node reaches for one of those, the
abstraction has failed and swapping providers becomes a rewrite.

Every call returns token counts and latency, because Phase 10 needs to answer
"where did the time and the money go?" and retrofitting that accounting means
touching every call site.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

SchemaT = TypeVar("SchemaT", bound=BaseModel)


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class Message:
    """One turn of a conversation."""

    role: Role
    content: str


@dataclass(frozen=True)
class Usage:
    """What a call cost.

    Recorded on every completion so Phase 10 can report tokens, latency and
    cost per node without any node having to remember to measure.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            latency_s=self.latency_s + other.latency_s,
        )


@dataclass(frozen=True)
class Completion:
    """A text response."""

    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"

    @property
    def was_truncated(self) -> bool:
        """Did the model stop because it ran out of room?

        Worth checking explicitly: a truncated response is usually still valid
        text, so it fails silently rather than raising. A half-written patch
        that parses is worse than one that errors.
        """
        return self.finish_reason.lower() in {"max_tokens", "length"}


@dataclass(frozen=True)
class StructuredCompletion[T: BaseModel]:
    """A response parsed into a declared schema."""

    value: T
    model: str
    usage: Usage = field(default_factory=Usage)
    raw_text: str = ""


class LLMError(Exception):
    """Base for provider failures."""


class LLMUnavailable(LLMError):
    """The provider could not be reached, or refused the request.

    Distinguished from a parsing failure because the responses differ: this one
    is worth retrying, a schema violation usually is not.
    """


class LLMResponseInvalid(LLMError):
    """The model replied, but not with what was asked for.

    The most common cause of this is not a broken provider — it is a prompt
    that under-specifies the output. Retrying identical input rarely helps.
    """


@runtime_checkable
class LLMProvider(Protocol):
    """What the rest of PatchPilot is allowed to ask of a language model.

    A Protocol rather than a base class: implementations do not inherit from
    anything, they simply have these methods. That keeps the fake provider in
    the test suite from being coupled to the real one.
    """

    @property
    def name(self) -> str:
        """Identifies the provider and model, e.g. ``gemini/gemini-2.0-flash``.

        Recorded in traces and benchmark results, so a metric can always be
        attributed to the model that produced it.
        """
        ...

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> Completion:
        """Generate free text."""
        ...

    def complete_structured(
        self,
        messages: list[Message],
        schema: type[SchemaT],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> StructuredCompletion[SchemaT]:
        """Generate a response conforming to ``schema``.

        This is the method that matters for an agent. A node that has to parse
        prose is a node that breaks when the model rewords its answer; a node
        receiving a validated object either works or raises.
        """
        ...


def user(content: str) -> Message:
    """Shorthand for a single user turn."""
    return Message(role=Role.USER, content=content)


def assistant(content: str) -> Message:
    return Message(role=Role.ASSISTANT, content=content)


def describe_usage(usage: Usage) -> dict[str, Any]:
    """Usage as log fields."""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "latency_s": round(usage.latency_s, 3),
    }
