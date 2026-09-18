# Configuration, and keeping secrets out of your repository

## In one sentence

Everything that changes between machines comes from environment variables,
validated once at startup, so a missing setting fails immediately instead of
twenty minutes into a run.

## The problem it solves

PatchPilot needs a GitHub token, a workspace directory, a log level, timeouts.
These differ per machine, and one of them is a secret.

The naive approach scatters `os.getenv("GITHUB_TOKEN")` through the codebase.
Four things go wrong:

1. **No validation.** A typo gives you `None`, which fails later somewhere
   unrelated.
2. **No types.** Every environment variable is a string. `os.getenv("TIMEOUT")`
   is `"30"`, and `"30" * 2` is `"3030"`.
3. **No inventory.** Nobody can answer "what can be configured?" without
   grepping.
4. **Late failure.** A missing token surfaces on the first API call, after
   you have already cloned a repository.

## How it works, step by step

One class declares everything, with types and defaults:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="PATCHPILOT_")

    github_token: SecretStr | None = None
    workspace_dir: Path = Path("workspace")
    clone_timeout_s: int = Field(default=300, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
```

Reading order: **environment variable → `.env` file → the default here.**

Three things this buys:

**Types are real.** `clone_timeout_s` arrives as `int`, not `"300"`. `gt=0`
means `PATCHPILOT_CLONE_TIMEOUT_S=0` is rejected rather than silently meaning
"give up instantly".

**Typos fail loudly.** `log_level` is a `Literal`, so `LOUD` raises at startup
with a clear message.

**One inventory.** [`config.py`](../../src/patchpilot/config.py) is the complete
answer to "what can be configured?"

### Secrets

`github_token` is a `SecretStr`, not a `str`. That single choice means:

```python
print(settings)                      # github_token=SecretStr('**********')
raise Exception(f"failed: {settings}")   # still masked
```

Tokens leak through logs, tracebacks, and error reports far more often than
through anyone reading the code. `SecretStr` makes the accidental path safe;
you must call `.get_secret_value()` to see it, and that call is greppable.

There is a test for this —
`test_token_is_not_exposed_by_repr` in
[`tests/unit/test_config.py`](../../tests/unit/test_config.py).

### The `.env` / `.env.example` pair

| File | Committed? | Contains |
|---|---|---|
| `.env` | **never** (gitignored) | your real token |
| `.env.example` | yes | the key names, with empty values |

`.env.example` is documentation that cannot go stale, because anyone setting up
the project copies it.

### Making a relative path absolute

```python
@field_validator("workspace_dir")
def _resolve_workspace(cls, value: Path) -> Path:
    return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()
```

Without this, `workspace` means "relative to wherever you ran the command."
Running the CLI from your home directory would clone into `~/workspace`. The
validator anchors it to the project.

## In PatchPilot

- [`src/patchpilot/config.py`](../../src/patchpilot/config.py) — the whole
  configuration surface.
- [`.env.example`](../../.env.example) — the template.
- [`.gitignore`](../../.gitignore) — `.env` is the first entry.

## What goes wrong

**Committing `.env`.** Git history is permanent. A token pushed once must be
revoked, not deleted.

**Required-at-import.** `settings = Settings()` runs on import, so declaring
`github_token` required would mean nobody can run the tests without a token. It
is optional here, and validated where it is actually needed. The tradeoff is
real and is [ADR-002](../adr/ADR-002-pydantic-settings.md).

**Module-level instantiation.** Importing anything reads the environment. That
is convenient, and it is why tests pass `_env_file=None` to get a clean object.
If it becomes a problem, the fix is a cached factory function.

## Interview questions

**Q: How do you handle configuration and secrets?**

One `pydantic-settings` class declaring every setting with a type and a default,
read from environment variables with a `.env` fallback for local development.
Validation happens once at startup so a bad value fails immediately. Secrets are
`SecretStr`, so they are masked in reprs, logs and tracebacks, and `.env` is
gitignored with a committed `.env.example` as the template.

**Q: Should a missing token fail at startup or at first use?**

Depends who you are optimising for. Failing at startup is clearest, but here it
would mean nobody can run the test suite without provisioning a token — GitHub's
API works unauthenticated for public repos, just at 60 requests an hour instead
of 5,000. So it is optional to import and validated at the point the higher
limit is actually needed. Required to *run*, optional to *import*.

**Q: Why `SecretStr` rather than a plain string?**

Because tokens leak through logs and tracebacks, not through people reading
source. `SecretStr` masks the value everywhere it is formatted, and forces an
explicit `.get_secret_value()` call to read it — which is greppable during a
review.

## Official documentation

- [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) — sources, precedence, prefixes.
- [Pydantic — `SecretStr`](https://docs.pydantic.dev/latest/api/types/#pydantic.types.SecretStr) — masking behaviour.
- [The Twelve-Factor App — Config](https://12factor.net/config) — why configuration belongs in the environment.
