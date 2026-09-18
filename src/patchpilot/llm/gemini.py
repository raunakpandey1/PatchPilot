"""Google Gemini provider.

Chosen because its free tier is the only one generous enough to run this
project's benchmark repeatedly without a budget. Everything above this file is
written against :class:`~patchpilot.llm.base.LLMProvider`, so that choice is a
line of configuration rather than an architectural commitment.

Two things this file is careful about
-------------------------------------
**Structured output.** Gemini accepts a Pydantic model as ``response_schema``
and enforces it server-side, returning parsed objects. That is meaningfully
stronger than asking for JSON in the prompt and hoping: the model is constrained
during generation rather than corrected afterwards.

**Usage accounting.** Every call records input and output tokens and wall-clock
latency. Phase 10 reports cost per node, and no node should have to remember to
measure.
"""

from __future__ import annotations

import time
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import ValidationError
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from patchpilot.llm.base import (
    Completion,
    LLMResponseInvalid,
    LLMUnavailable,
    Message,
    Role,
    SchemaT,
    StructuredCompletion,
    Usage,
)
from patchpilot.logging import get_logger

log = get_logger("llm.gemini")

# HTTP statuses worth retrying: rate limiting and server-side failures.
# A 400 means our request was wrong, and sending it again will not help.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class GeminiProvider:
    """Implements :class:`~patchpilot.llm.base.LLMProvider` for Gemini."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gemini-2.0-flash",
        max_attempts: int = 4,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 30.0,
        client: Any | None = None,
    ) -> None:
        # `client` is injectable so tests can substitute a stub without a key.
        self._client = client if client is not None else genai.Client(api_key=api_key)
        self._model = model
        self._retrying = Retrying(
            retry=retry_if_exception_type(LLMUnavailable),
            wait=wait_exponential_jitter(initial=backoff_initial_s, max=backoff_max_s),
            stop=stop_after_attempt(max_attempts),
            reraise=True,
        )

    @property
    def name(self) -> str:
        return f"gemini/{self._model}"

    # --- public API --------------------------------------------------------

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> Completion:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        response, usage = self._generate(messages, config)

        text = response.text or ""
        finish_reason = _finish_reason(response)
        if not text and finish_reason not in {"stop", "max_tokens"}:
            # An empty body with an unusual finish reason is normally a safety
            # filter. Surfacing it as an error beats returning "" and letting a
            # downstream node treat silence as an answer.
            raise LLMResponseInvalid(
                f"Gemini returned no text (finish_reason={finish_reason}). "
                f"This is usually a safety filter or a blocked prompt."
            )

        return Completion(
            text=text, model=self.name, usage=usage, finish_reason=finish_reason
        )

    def complete_structured(
        self,
        messages: list[Message],
        schema: type[SchemaT],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> StructuredCompletion[SchemaT]:
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_schema=schema,
        )
        response, usage = self._generate(messages, config)

        # The SDK parses into the schema for us when it can.
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            return StructuredCompletion(
                value=parsed, model=self.name, usage=usage, raw_text=response.text or ""
            )

        # Fall back to validating the raw text. This is the path taken when the
        # response was truncated mid-JSON — worth a distinct error message,
        # because the fix is a larger `max_output_tokens`, not a better prompt.
        raw = response.text or ""
        try:
            value = schema.model_validate_json(raw)
        except ValidationError as exc:
            truncated = _finish_reason(response) == "max_tokens"
            hint = (
                " The response hit max_output_tokens, so the JSON is incomplete."
                if truncated
                else ""
            )
            raise LLMResponseInvalid(
                f"Gemini response did not match {schema.__name__}.{hint} "
                f"First 300 chars: {raw[:300]!r}"
            ) from exc

        return StructuredCompletion(value=value, model=self.name, usage=usage, raw_text=raw)

    # --- internals ---------------------------------------------------------

    def _generate(
        self, messages: list[Message], config: types.GenerateContentConfig
    ) -> tuple[Any, Usage]:
        contents = _to_contents(messages)
        started = time.monotonic()
        response = self._retrying(self._generate_once, contents, config)
        latency = time.monotonic() - started

        usage = _usage_from(response, latency)
        log.debug(
            "llm_call",
            model=self._model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_s=round(latency, 3),
        )
        return response, usage

    def _generate_once(
        self, contents: list[types.Content], config: types.GenerateContentConfig
    ) -> Any:
        try:
            return self._client.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except genai_errors.APIError as exc:
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            if status in RETRYABLE_STATUS:
                # Raised as LLMUnavailable so the retry policy picks it up; a
                # 400 falls through to the non-retryable branch below.
                raise LLMUnavailable(f"Gemini returned {status}: {exc}") from exc
            raise LLMResponseInvalid(f"Gemini rejected the request ({status}): {exc}") from exc
        except Exception as exc:  # network-level failures
            raise LLMUnavailable(f"Could not reach Gemini: {exc}") from exc


def _to_contents(messages: list[Message]) -> list[types.Content]:
    """Translate our messages into Gemini's shape.

    Gemini calls the assistant role ``model``. Mapping it here, in the adapter,
    is the whole point of having an adapter — no node should know this.
    """
    role_map = {Role.USER: "user", Role.ASSISTANT: "model"}
    return [
        types.Content(role=role_map[m.role], parts=[types.Part.from_text(text=m.content)])
        for m in messages
    ]


def _usage_from(response: Any, latency_s: float) -> Usage:
    meta = getattr(response, "usage_metadata", None)
    return Usage(
        input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
        output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        latency_s=latency_s,
    )


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "none"
    reason = getattr(candidates[0], "finish_reason", None)
    if reason is None:
        return "stop"
    name = getattr(reason, "name", str(reason))
    return str(name).lower()
