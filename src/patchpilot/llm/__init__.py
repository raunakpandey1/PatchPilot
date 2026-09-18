"""Language model providers.

Everything outside this package depends on the ``LLMProvider`` protocol, never
on a concrete provider. :func:`build_provider` is the single place a provider is
chosen, so swapping models is a configuration change.
"""

from __future__ import annotations

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

    from patchpilot.llm.gemini import GeminiProvider

    return GeminiProvider(
        api_key=config.gemini_api_key.get_secret_value(),
        model=config.llm_model,
        max_attempts=config.llm_max_attempts,
    )
