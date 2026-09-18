"""Application configuration.

One place that answers "what can be configured, and what happens if it isn't?"

Values come from environment variables prefixed with ``PATCHPILOT_``, falling
back to a local ``.env`` file, falling back to the defaults declared here.
Everything is validated once, at import time, so a bad configuration fails
immediately instead of 20 minutes into an agent run.
"""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The project root: .../patchpilot/src/patchpilot/config.py -> .../patchpilot
PROJECT_ROOT = Path(__file__).resolve().parents[2]

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PATCHPILOT_",
        extra="ignore",
    )

    # --- GitHub ------------------------------------------------------------
    # Optional on purpose: public repos work unauthenticated, just at 60
    # requests/hour instead of 5,000. Making this required would mean nobody
    # can run the test suite without provisioning a token.
    github_token: SecretStr | None = None

    # Configurable so tests can point at a local stub server, and so GitHub
    # Enterprise works later without a code change.
    github_api_url: str = "https://api.github.com"

    # --- Workspace ---------------------------------------------------------
    # Where cloned repositories land. Everything we write to disk stays under
    # this directory, which is what makes "confine untrusted code" enforceable.
    workspace_dir: Path = Path("workspace")

    # --- Safety budgets ----------------------------------------------------
    # A repository is untrusted input. It may be enormous, or may hang.
    clone_timeout_s: int = Field(default=300, gt=0)
    max_repo_size_mb: int = Field(default=500, gt=0)
    http_timeout_s: float = Field(default=30.0, gt=0)

    # --- Language model ----------------------------------------------------
    # Which provider to construct. The rest of the system only ever sees the
    # LLMProvider protocol, so this is the only place the choice appears.
    llm_provider: Literal["gemini", "fake"] = "gemini"
    gemini_api_key: SecretStr | None = None
    llm_model: str = "gemini-2.0-flash"
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_output_tokens: int = Field(default=4096, gt=0)
    llm_max_attempts: int = Field(default=4, ge=1)

    # --- Agent -------------------------------------------------------------
    # Where LangGraph checkpoints live. Deleting this file loses the ability to
    # resume paused runs; it holds no other state.
    checkpoint_db: Path = Path("workspace/checkpoints.sqlite")
    max_debug_iterations: int = Field(default=5, ge=1, le=20)

    # --- Observability -----------------------------------------------------
    log_level: LogLevel = "INFO"

    @field_validator("workspace_dir", "checkpoint_db")
    @classmethod
    def _resolve_under_project(cls, value: Path) -> Path:
        """Make relative paths absolute against the project root.

        Without this, the workspace location would depend on the current
        working directory — so running the CLI from a different folder would
        silently clone into the wrong place.
        """
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()


settings = Settings()
