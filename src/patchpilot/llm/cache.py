"""Caching and budgeting for model calls.

Why both, and why now
---------------------
This project runs on a free tier with a hard daily call limit. Two distinct
problems follow, and they need two distinct mechanisms.

**Repetition.** During development the same prompt is sent many times — re-run
a benchmark, re-run an agent after a code change, re-run after a crash. Every
one of those repeats costs quota to obtain an answer already obtained. A cache
keyed on the exact request makes a repeat free, which is what allows a fixed
pipeline to be iterated on at all.

**Runaway loops.** Phase 7's debug loop calls the model until the tests pass or
a bound is hit. A bug in that bound — or an unexpectedly long benchmark — can
spend a day's quota in minutes, with nothing to show. A budget makes that
impossible rather than unlikely.

What makes a cache key
----------------------
Everything that can change the answer: the model, the system prompt, the
messages, the schema, the temperature, and the output limit. Leave any of them
out and the cache will confidently serve an answer to a different question —
which is worse than no cache, because it is invisible.

Note that this is only sound because PatchPilot calls models at
``temperature=0``. At a higher temperature the same request is *supposed* to
give different answers, and caching would quietly remove the variation the
caller asked for.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from patchpilot.llm.base import (
    Completion,
    LLMError,
    LLMProvider,
    Message,
    SchemaT,
    StructuredCompletion,
    Usage,
)
from patchpilot.logging import get_logger

log = get_logger("llm.cache")


class BudgetExceeded(LLMError):
    """The configured call budget is spent.

    Deliberately an error rather than a silent stop: a run that quietly does
    less work than asked is harder to notice than one that fails.
    """


class CallBudget:
    """A hard ceiling on model calls.

    Counts only calls that actually reach a provider — a cache hit is free and
    does not consume budget, which is the whole point of pairing the two.
    """

    def __init__(self, max_calls: int | None = None) -> None:
        self.max_calls = max_calls
        self.calls_made = 0
        self.tokens_used = 0

    @property
    def remaining(self) -> float:
        return float("inf") if self.max_calls is None else self.max_calls - self.calls_made

    def check(self) -> None:
        if self.max_calls is not None and self.calls_made >= self.max_calls:
            raise BudgetExceeded(
                f"call budget exhausted ({self.calls_made}/{self.max_calls}). "
                f"Raise PATCHPILOT_LLM_MAX_CALLS or re-run — cached calls are free."
            )

    def record(self, usage: Usage) -> None:
        self.calls_made += 1
        self.tokens_used += usage.total_tokens

    def summary(self) -> dict[str, int | None]:
        return {
            "calls_made": self.calls_made,
            "max_calls": self.max_calls,
            "tokens_used": self.tokens_used,
        }


class CachingProvider:
    """Wraps a provider with an on-disk response cache and a call budget.

    Implements :class:`~patchpilot.llm.base.LLMProvider`, so nothing downstream
    knows the difference — which is the point of the protocol.
    """

    def __init__(
        self,
        inner: LLMProvider,
        cache_dir: Path,
        *,
        budget: CallBudget | None = None,
        enabled: bool = True,
    ) -> None:
        self._inner = inner
        # A *stable* identity for the key, resolved once.
        #
        # Not `inner.name`, which for a FallbackProvider reports whichever model
        # last answered and therefore changes after any fallback. Using it would
        # give the same request a different key on the next call, so the cache
        # would never hit — silently, while appearing to work.
        #
        # `chain` is the stable answer for a fallback chain: the set of models
        # that could serve this request. For a single provider the name is
        # already stable, and capturing it once makes that explicit.
        chain = getattr(inner, "chain", None)
        self._identity = "|".join(chain) if chain else inner.name
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self.budget = budget or CallBudget()
        self._enabled = enabled
        self.hits = 0
        self.misses = 0

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def stats(self) -> dict[str, int | None]:
        return {"cache_hits": self.hits, "cache_misses": self.misses, **self.budget.summary()}

    # --- keys and storage --------------------------------------------------

    def _key(
        self,
        messages: list[Message],
        system: str | None,
        schema_name: str | None,
        temperature: float,
        max_output_tokens: int,
    ) -> str:
        payload = json.dumps(
            {
                "provider": self._identity,
                "system": system,
                "messages": [[str(m.role), m.content] for m in messages],
                "schema": schema_name,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:40]

    def _read(self, key: str) -> dict[str, Any] | None:
        """Deserialised JSON, so ``Any`` is the honest value type — the shape is
        ours but the parser cannot know that."""
        path = self._dir / f"{key}.json"
        if not self._enabled or not path.exists():
            return None
        try:
            data: dict[str, Any] = json.loads(path.read_text())
            return data
        except (json.JSONDecodeError, OSError):
            # A corrupt entry must never break a run. Treat it as a miss; the
            # next successful response overwrites it.
            log.warning("cache_entry_unreadable", key=key)
            return None

    def _write(self, key: str, data: dict[str, Any]) -> None:
        if not self._enabled:
            return
        path = self._dir / f"{key}.json"
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data))
            tmp.replace(path)  # atomic on POSIX
        except OSError:
            log.warning("cache_write_failed", key=key)

    # --- the provider interface --------------------------------------------

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> Completion:
        key = self._key(messages, system, None, temperature, max_output_tokens)

        if (cached := self._read(key)) is not None:
            self.hits += 1
            log.debug("llm_cache_hit", key=key)
            return Completion(
                text=cached["text"],
                model=cached["model"],
                usage=Usage(**cached["usage"]),
                finish_reason=cached.get("finish_reason", "stop"),
            )

        self.budget.check()
        self.misses += 1
        result = self._inner.complete(
            messages, system=system, temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        self.budget.record(result.usage)
        self._write(key, {
            "text": result.text,
            "model": result.model,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "latency_s": result.usage.latency_s,
            },
            "finish_reason": result.finish_reason,
        })
        return result

    def complete_structured(
        self,
        messages: list[Message],
        schema: type[SchemaT],
        *,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
    ) -> StructuredCompletion[SchemaT]:
        key = self._key(messages, system, schema.__name__, temperature, max_output_tokens)

        if (cached := self._read(key)) is not None:
            self.hits += 1
            log.debug("llm_cache_hit", key=key, schema=schema.__name__)
            return StructuredCompletion(
                value=schema.model_validate_json(cached["raw_text"]),
                model=cached["model"],
                usage=Usage(**cached["usage"]),
                raw_text=cached["raw_text"],
            )

        self.budget.check()
        self.misses += 1
        result = self._inner.complete_structured(
            messages, schema, system=system, temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        self.budget.record(result.usage)
        self._write(key, {
            "raw_text": result.value.model_dump_json(),
            "model": result.model,
            "usage": {
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "latency_s": result.usage.latency_s,
            },
        })
        return result
