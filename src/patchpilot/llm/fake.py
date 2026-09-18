"""A scripted language model, for tests.

Why this exists
---------------
Every unit test in PatchPilot runs offline, in milliseconds, for free, and gives
the same answer every time. A real model gives none of those four.

A fake also makes testable the things that are hard to trigger deliberately
against a real model:

* the model returns something that does not match the schema
* the model is unavailable
* the model repeats itself forever (which is what the Phase 7 debug loop must
  survive)

And it records what it was asked, so a test can assert on the prompt — that
retrieved context actually reached the model, for instance — rather than only on
the output.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ValidationError

from patchpilot.llm.base import (
    Completion,
    LLMResponseInvalid,
    LLMUnavailable,
    Message,
    SchemaT,
    StructuredCompletion,
    Usage,
)


@dataclass
class RecordedCall:
    """One request the fake received."""

    messages: list[Message]
    system: str | None
    schema: str | None
    temperature: float

    @property
    def prompt_text(self) -> str:
        """Everything the model was shown, as one string.

        Lets a test assert `"sqlite_utils/db.py" in call.prompt_text` — that the
        retrieved context genuinely made it into the prompt.
        """
        parts = [self.system or ""]
        parts += [m.content for m in self.messages]
        return "\n".join(p for p in parts if p)


@dataclass
class FakeProvider:
    """A provider that returns whatever you scripted.

    ``responses`` may contain:

    * ``str`` — returned as completion text
    * a ``BaseModel`` instance — returned from ``complete_structured``
    * an ``Exception`` — raised, to simulate provider failure

    Responses are consumed in order. When the script runs out, the last entry
    repeats — which is how you simulate a model that will not stop saying the
    same wrong thing.
    """

    responses: Sequence[Any] = field(default_factory=list)
    model_name: str = "fake/deterministic"
    input_tokens: int = 100
    output_tokens: int = 50
    latency_s: float = 0.0
    on_call: Callable[[RecordedCall], None] | None = None

    calls: list[RecordedCall] = field(default_factory=list, init=False)
    _index: int = field(default=0, init=False)

    @property
    def name(self) -> str:
        return self.model_name

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_call(self) -> RecordedCall:
        if not self.calls:
            raise AssertionError("the provider was never called")
        return self.calls[-1]

    def _next_response(self) -> Any:
        if not self.responses:
            return ""
        # Past the end, repeat the last entry rather than raising. A test for
        # "the debug loop gives up after N attempts" needs a model that keeps
        # answering, not one that explodes on attempt four.
        index = min(self._index, len(self.responses) - 1)
        self._index += 1
        return self.responses[index]

    def _record(
        self,
        messages: list[Message],
        system: str | None,
        schema: type[BaseModel] | None,
        temperature: float,
    ) -> None:
        call = RecordedCall(
            messages=list(messages),
            system=system,
            schema=schema.__name__ if schema else None,
            temperature=temperature,
        )
        self.calls.append(call)
        if self.on_call:
            self.on_call(call)

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> Completion:
        self._record(messages, system, None, temperature)
        response = self._next_response()

        if isinstance(response, Exception):
            raise response
        text = response if isinstance(response, str) else json.dumps(_to_jsonable(response))

        return Completion(
            text=text,
            model=self.model_name,
            usage=Usage(self.input_tokens, self.output_tokens, self.latency_s),
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
        self._record(messages, system, schema, temperature)
        response = self._next_response()

        if isinstance(response, Exception):
            raise response

        if isinstance(response, schema):
            value = response
        elif isinstance(response, str):
            # A scripted string here means "the model returned raw text where a
            # schema was expected" — the failure mode worth testing.
            try:
                value = schema.model_validate_json(response)
            except ValidationError as exc:
                raise LLMResponseInvalid(
                    f"Fake response did not match {schema.__name__}: {exc}"
                ) from exc
        else:
            raise LLMUnavailable(
                f"FakeProvider was scripted with {type(response).__name__}, "
                f"which is neither {schema.__name__}, a str, nor an Exception."
            )

        return StructuredCompletion(
            value=value,
            model=self.model_name,
            usage=Usage(self.input_tokens, self.output_tokens, self.latency_s),
            raw_text=value.model_dump_json(),
        )


def _to_jsonable(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value
