"""Config tests.

These also prove the src/ layout works: if `import patchpilot` resolves here,
it resolved through the installed package, not a directory that happened to be
sitting in the current working directory.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from patchpilot.config import PROJECT_ROOT, Settings


def test_defaults_allow_running_without_any_env(monkeypatch):
    """A fresh checkout must be runnable with no .env and no exported vars."""
    monkeypatch.delenv("PATCHPILOT_LOG_LEVEL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.log_level == "INFO"
    assert settings.github_token is None
    assert settings.github_api_url == "https://api.github.com"


def test_relative_workspace_resolves_against_project_root():
    """Otherwise the clone location depends on the current directory."""
    settings = Settings(_env_file=None, workspace_dir=Path("workspace"))

    assert settings.workspace_dir.is_absolute()
    assert settings.workspace_dir == PROJECT_ROOT / "workspace"


def test_absolute_workspace_is_left_alone(tmp_path):
    settings = Settings(_env_file=None, workspace_dir=tmp_path)

    assert settings.workspace_dir == tmp_path


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("PATCHPILOT_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PATCHPILOT_GITHUB_TOKEN", "ghp_notarealtoken")

    settings = Settings(_env_file=None)

    assert settings.log_level == "DEBUG"
    assert settings.github_token.get_secret_value() == "ghp_notarealtoken"


def test_token_is_not_exposed_by_repr(monkeypatch):
    """A leaked token in a traceback or log line is a real incident."""
    monkeypatch.setenv("PATCHPILOT_GITHUB_TOKEN", "ghp_notarealtoken")

    settings = Settings(_env_file=None)

    assert "ghp_notarealtoken" not in repr(settings)
    assert "ghp_notarealtoken" not in str(settings.github_token)


def test_invalid_log_level_fails_fast():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="LOUD")


def test_non_positive_budget_fails_fast():
    """A zero timeout would mean 'give up instantly', almost certainly a typo."""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, clone_timeout_s=0)
