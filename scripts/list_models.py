"""List the models this API key can actually reach.

Exists because model IDs are not stable. `gemini-2.0-flash` was the documented
default when this project's LLM layer was written and returns 404 now, with the
API pointing at a successor. Guessing a model name from memory or from a blog
post is how you get a confusing failure weeks later.

    poetry run python scripts/list_models.py
"""

from __future__ import annotations

from google import genai

from patchpilot.config import settings


def main() -> None:
    if settings.gemini_api_key is None:
        raise SystemExit(
            "PATCHPILOT_GEMINI_API_KEY is not set. Get a free key at "
            "https://aistudio.google.com/apikey"
        )

    client = genai.Client(api_key=settings.gemini_api_key.get_secret_value())
    usable = [
        model
        for model in client.models.list()
        if "generateContent" in (model.supported_actions or [])
    ]

    print(f"{len(usable)} models support generateContent\n")
    print(f"{'model':<44} {'input':>9} {'output':>9}")
    print("-" * 64)
    for model in sorted(usable, key=lambda m: m.name):
        print(
            f"{model.name.replace('models/', ''):<44} "
            f"{model.input_token_limit or 0:>9,} {model.output_token_limit or 0:>9,}"
        )

    print(f"\nCurrently configured: {settings.llm_model}")
    print(f"Fast/bulk model:      {settings.llm_model_fast}")


if __name__ == "__main__":
    main()
