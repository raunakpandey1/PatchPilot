"""Language model providers.

Everything outside this package depends on the ``LLMProvider`` protocol, never
on a concrete provider. :func:`build_provider` is the single place a provider is
chosen, so swapping models is a configuration change.
"""

from __future__ import annotations

from pathlib import Path

from patchpilot.config import Settings
from patchpilot.config import settings as default_settings
from patchpilot.llm.base import (
    Completion,
    LLMError,
    LLMProvider,
    LLMResponseInvalid,
    LLMUnavailable,
    Message,
    Role,
    StructuredCompletion,
    Usage,
    assistant,
    user,
)

__all__ = [
    "Completion",
    "LLMError",
    "LLMProvider",
    "LLMResponseInvalid",
    "LLMUnavailable",
    "Message",
    "Role",
    "StructuredCompletion",
    "Usage",
    "assistant",
    "build_provider",
    "user",
]


def build_provider(settings: Settings | None = None) -> LLMProvider:
    """Construct the configured provider.

    Deliberately the only function in the codebase that names a concrete
    provider class. If a node ever imports ``GeminiProvider`` directly, the
    abstraction has been defeated and swapping models becomes a rewrite.
    """
    config = settings or default_settings

    if config.llm_provider == "fake":
        from patchpilot.llm.fake import FakeProvider

        return FakeProvider()

    if config.gemini_api_key is None:
        raise LLMUnavailable(
            "PATCHPILOT_GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey, or set PATCHPILOT_LLM_PROVIDER=fake."
        )

    from patchpilot.llm.fallback import FallbackProvider
    from patchpilot.llm.gemini import GeminiProvider

    key = config.gemini_api_key.get_secret_value()

    # A chain rather than a single model, because free-tier capacity is real:
    # on the first live run the primary returned 503 for several minutes while
    # other models served normally. Fallback is on unavailability only — see
    # llm/fallback.py for why a bad response must not trigger it.
    chain = [
        GeminiProvider(api_key=key, model=name, max_attempts=config.llm_max_attempts)
        for name in dict.fromkeys(
            [config.llm_model, *config.llm_fallback_models, config.llm_model_fast]
        )
    ]
    # Cache outermost: a cache hit must not consume the call budget, and must
    # not care which model in the chain originally answered.
    from patchpilot.llm.cache import CachingProvider, CallBudget

    return CachingProvider(
        FallbackProvider(chain),
        cache_dir=Path(config.workspace_dir) / "llm_cache",
        budget=CallBudget(config.llm_max_calls),
        enabled=config.llm_cache_enabled,
    )
