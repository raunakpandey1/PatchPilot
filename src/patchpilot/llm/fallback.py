"""Falling back to another model when the primary one is unavailable.

Why this exists
---------------
Not as a theoretical reliability exercise. On the first real end-to-end run,
`gemini-3.8-flash` returned::

    503 UNAVAILABLE — This model is currently experiencing high demand.

The retry policy did the right thing: it backed off exponentially, tried four
times over 38 seconds, and then halted with a clear reason. But retrying a
*single* overloaded model cannot help when the overload lasts minutes. Measured
in that moment, availability varied by model:

    gemini-3.8-flash        503
    gemini-3.7-flash        503
    gemini-3.6-flash        OK   (10.9 s)
    gemini-3.5-flash-lite   OK   ( 1.4 s)

So the useful move is not "retry harder", it is "ask a different model".

The important distinction
-------------------------
**Fall back on unavailability, never on a bad response.**

* ``LLMUnavailable`` — 503, 429, a network failure. The request was fine; the
  service could not serve it. **Another model probably can.** Fall back.
* ``LLMResponseInvalid`` — a 400, or output that does not match the schema. The
  request or the prompt is wrong, and it will be just as wrong at the next
  model. Falling back here would spend three models' quota to collect three
  copies of the same failure, and would hide the real bug.

Getting this backwards is the classic failure of retry-and-fallback code: it
turns a loud, fixable error into a slow, silent one.

The honest cost
---------------
Results become model-dependent. A benchmark row produced by the fallback is not
comparable with one produced by the primary, so ``last_used`` records which
model actually answered, and every measurement records it alongside the number.
A metric without the model that produced it is not a metric.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from patchpilot.llm.base import (
    Completion,
    LLMProvider,
    LLMUnavailable,
    Message,
    SchemaT,
    StructuredCompletion,
)
from patchpilot.logging import get_logger

log = get_logger("llm.fallback")

# What a single attempt returns — a Completion or a StructuredCompletion. The
# chain does not care which; it only distinguishes "answered" from "unavailable".
ResultT = TypeVar("ResultT")


class FallbackProvider:
    """Try each provider in order until one answers.

    Implements :class:`~patchpilot.llm.base.LLMProvider`, so nothing downstream
    knows it is talking to a chain rather than a single model.
    """

    def __init__(self, providers: Sequence[LLMProvider]) -> None:
        if not providers:
            raise ValueError("FallbackProvider needs at least one provider")
        self._providers = list(providers)
        self._last_used: LLMProvider = self._providers[0]
        self._fallback_count = 0

    @property
    def name(self) -> str:
        """The model that actually answered most recently.

        Deliberately *not* a static description of the chain. Callers record
        this against their results, and what they need to know is which model
        produced the answer in front of them — not which ones were configured.
        """
        return self._last_used.name

    @property
    def chain(self) -> tuple[str, ...]:
        return tuple(p.name for p in self._providers)

    @property
    def fallback_count(self) -> int:
        """How many times the primary was skipped. Surfaced in run metadata so a
        slow or odd result can be attributed to degraded service."""
        return self._fallback_count

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> Completion:
        def call(provider: LLMProvider) -> Completion:
            return provider.complete(
                messages, system=system, temperature=temperature,
                max_output_tokens=max_output_tokens,
            )

        return self._attempt(call)

    def complete_structured(
        self,
        messages: list[Message],
        schema: type[SchemaT],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> StructuredCompletion[SchemaT]:
        def call(provider: LLMProvider) -> StructuredCompletion[SchemaT]:
            return provider.complete_structured(
                messages, schema, system=system, temperature=temperature,
                max_output_tokens=max_output_tokens,
            )

        return self._attempt(call)

    def _attempt(self, call: Callable[[LLMProvider], ResultT]) -> ResultT:
        last_error: LLMUnavailable | None = None

        for index, provider in enumerate(self._providers):
            try:
                result = call(provider)
            except LLMUnavailable as exc:
                # Service-side failure. Another model may well succeed.
                last_error = exc
                log.warning(
                    "llm_unavailable_falling_back",
                    provider=provider.name,
                    remaining=len(self._providers) - index - 1,
                    error=str(exc)[:160],
                )
                continue
            # Note: LLMResponseInvalid is deliberately NOT caught. A malformed
            # request or a schema violation will fail identically everywhere,
            # and swallowing it here would hide a real bug behind a slow one.

            if index > 0:
                self._fallback_count += 1
                log.info("llm_fallback_succeeded", provider=provider.name, position=index)
            self._last_used = provider
            return result

        raise LLMUnavailable(
            f"every provider in the chain was unavailable "
            f"({', '.join(self.chain)}). Last error: {last_error}"
        )
