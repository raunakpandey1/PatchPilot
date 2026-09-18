"""Characterise a cloned repository — deterministically.

The question this answers: *what kind of project is this, and how do I run its
tests?*

Why no LLM here
---------------
Every fact in this module is already written down in the repository. The test
runner is declared in ``pyproject.toml`` or ``tox.ini``. The Python version is
in ``requires-python``. Asking a language model to read those files would cost
money, take a second, return a slightly different answer each time, and
occasionally invent a command that does not exist.

That last failure is the expensive one. If this module reports
``pytest --cov`` for a project that does not have ``pytest-cov`` installed, the
sandbox in Phase 6 runs a command that fails for reasons unrelated to the
patch, and the debug loop in Phase 7 spends several LLM calls "fixing" a bug
that was never in the code.

**Look things up when they are written down. Use the model only for judgement.**
"""

from __future__ import annotations

import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from patchpilot.logging import get_logger
from patchpilot.models import (
    PackageManager,
    Repository,
    RepositorySnapshot,
    TestRunner,
)
from patchpilot.tools.git import GitRepository

log = get_logger("analysis")

# Parsed pyproject.toml. Its shape is whatever the repository wrote, so the
# value type is deliberately `Any` — every read below is defensive.
PyProject = dict[str, Any]

LANGUAGE_BY_EXTENSION = {
    ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
    ".jsx": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby",
    ".c": "C", ".h": "C", ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".php": "PHP",
    ".sh": "Shell", ".sql": "SQL",
}

DOC_EXTENSIONS = {".md", ".rst", ".txt", ".adoc"}
CONFIG_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "noxfile.py",
    "requirements.txt", "requirements-dev.txt", "Pipfile", "package.json",
    "Makefile", "Dockerfile", ".pre-commit-config.yaml",
}


def analyze_repository(
    repository: Repository,
    clone: GitRepository,
) -> RepositorySnapshot:
    """Read a clone and produce the snapshot every later phase depends on."""
    files = clone.list_files()
    pyproject = _load_pyproject(clone.path)

    source_files, test_files, doc_files, config_files = _classify(files)
    languages = _detect_languages(files)
    package_manager = _detect_package_manager(clone.path, pyproject)
    test_runner = _detect_test_runner(clone.path, pyproject, test_files)

    snapshot = RepositorySnapshot(
        repository=repository,
        local_path=clone.path,
        head_sha=clone.head_sha,
        languages=languages,
        package_manager=package_manager,
        test_runner=test_runner,
        test_command=_detect_test_command(test_runner, pyproject, test_files),
        lint_command=_detect_lint_command(clone.path, pyproject),
        typecheck_command=_detect_typecheck_command(clone.path, pyproject),
        source_files=source_files,
        test_files=test_files,
        doc_files=doc_files,
        config_files=config_files,
        python_requires=_python_requires(pyproject),
        has_ci=(clone.path / ".github" / "workflows").is_dir(),
        total_files=len(files),
        total_bytes=int((clone.size_mb or 0) * 1024 * 1024),
    )

    log.info(
        "repository_analyzed",
        repo=repository.full_name,
        files=snapshot.total_files,
        source=len(source_files),
        tests=len(test_files),
        package_manager=str(package_manager),
        test_runner=str(test_runner),
        testable=snapshot.is_testable,
    )
    return snapshot


# --- file classification ----------------------------------------------------


def _classify(files: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Split tracked files into source / test / doc / config.

    Test files are separated first and deliberately. Two later phases depend on
    it: RAG should weight production code above test fixtures when answering
    "where is this bug", and the patch generator must know which files are
    tests so it cannot "fix" a failure by deleting the test that caught it.
    """
    source: list[str] = []
    tests: list[str] = []
    docs: list[str] = []
    configs: list[str] = []

    for path in files:
        p = Path(path)
        suffix = p.suffix.lower()

        if _is_test_file(p):
            tests.append(path)
        elif p.name in CONFIG_NAMES or suffix in {".toml", ".cfg", ".ini", ".yaml", ".yml"}:
            configs.append(path)
        elif suffix in DOC_EXTENSIONS:
            docs.append(path)
        elif suffix in LANGUAGE_BY_EXTENSION:
            source.append(path)

    return tuple(source), tuple(tests), tuple(docs), tuple(configs)


def _is_test_file(path: Path) -> bool:
    return is_test_path(str(path))


def is_test_path(file_path: str) -> bool:
    """Is this path a test file?

    Public because the retrieval evaluator needs the same rule. Two copies of
    "what counts as a test" would drift, and the drift would show up as an
    unexplainable change in a benchmark number.
    """
    path = Path(file_path)
    parts = {part.lower() for part in path.parts}
    if parts & {"tests", "test", "testing"}:
        return True
    name = path.name.lower()
    return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def _detect_languages(files: tuple[str, ...]) -> tuple[str, ...]:
    """Languages present, most common first, ignoring the long tail.

    The 5% floor keeps a single stray ``.sh`` from being reported as though the
    project were partly a shell project.
    """
    counts = Counter(
        LANGUAGE_BY_EXTENSION[Path(f).suffix.lower()]
        for f in files
        if Path(f).suffix.lower() in LANGUAGE_BY_EXTENSION
    )
    total = sum(counts.values())
    if not total:
        return ()
    return tuple(lang for lang, n in counts.most_common() if n / total >= 0.05)


# --- toolchain detection ----------------------------------------------------


def _load_pyproject(root: Path) -> PyProject:
    """Parse ``pyproject.toml`` if present.

    ``tomllib`` is in the standard library from Python 3.11 — one of the
    reasons this project pins a modern Python.
    """
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        # A malformed pyproject is the repository's problem, not a crash for us.
        log.warning("pyproject_unparseable", path=str(path))
        return {}


def _detect_package_manager(root: Path, pyproject: PyProject) -> PackageManager:
    tool = pyproject.get("tool", {})
    build_backend = pyproject.get("build-system", {}).get("build-backend", "")

    if "poetry" in tool or "poetry" in build_backend:
        return PackageManager.POETRY
    if "pdm" in tool or "pdm" in build_backend:
        return PackageManager.PDM
    if "hatch" in tool or "hatchling" in build_backend:
        return PackageManager.HATCH
    if (root / "uv.lock").is_file():
        return PackageManager.UV
    if "setuptools" in build_backend or (root / "setup.py").is_file():
        return PackageManager.SETUPTOOLS
    if (root / "requirements.txt").is_file():
        return PackageManager.PIP
    return PackageManager.UNKNOWN


def _detect_test_runner(
    root: Path, pyproject: PyProject, test_files: tuple[str, ...]
) -> TestRunner:
    """Detect the test runner from evidence, strongest signal first."""
    # Strongest: an explicit pytest configuration section.
    if "pytest" in pyproject.get("tool", {}):
        return TestRunner.PYTEST
    for config_name in ("pytest.ini", "tox.ini", "setup.cfg"):
        path = root / config_name
        if path.is_file() and "[pytest]" in path.read_text(errors="replace"):
            return TestRunner.PYTEST
    # Next: a conftest.py exists only for pytest.
    if any(Path(f).name == "conftest.py" for f in test_files):
        return TestRunner.PYTEST
    # Weakest: pytest named anywhere in dependencies.
    if _mentions_dependency(pyproject, "pytest"):
        return TestRunner.PYTEST
    if test_files:
        return TestRunner.UNITTEST
    return TestRunner.UNKNOWN


def _mentions_dependency(pyproject: PyProject, name: str) -> bool:
    blob = str(pyproject.get("project", {}).get("dependencies", ""))
    blob += str(pyproject.get("project", {}).get("optional-dependencies", ""))
    blob += str(pyproject.get("dependency-groups", ""))
    blob += str(pyproject.get("tool", {}).get("poetry", {}))
    return name in blob


def _detect_test_command(
    framework: TestRunner, pyproject: PyProject, test_files: tuple[str, ...]
) -> tuple[str, ...] | None:
    """The command the sandbox will run.

    Returned as a tuple of arguments, never a string. A string would have to be
    handed to a shell to be run, and a shell interprets ``;``, ``&&`` and
    backticks — in a value derived from an untrusted repository. An argument
    list is passed straight to ``execve`` with no interpretation.
    """
    if not test_files:
        return None
    if framework is TestRunner.PYTEST:
        return ("python", "-m", "pytest", "-x", "-q", "--no-header")
    if framework is TestRunner.UNITTEST:
        return ("python", "-m", "unittest", "discover")
    return None


def _detect_lint_command(root: Path, pyproject: PyProject) -> tuple[str, ...] | None:
    tool = pyproject.get("tool", {})
    if "ruff" in tool or (root / "ruff.toml").is_file():
        return ("python", "-m", "ruff", "check", ".")
    if "flake8" in tool or (root / ".flake8").is_file():
        return ("python", "-m", "flake8")
    return None


def _detect_typecheck_command(root: Path, pyproject: PyProject) -> tuple[str, ...] | None:
    tool = pyproject.get("tool", {})
    if "mypy" in tool or (root / "mypy.ini").is_file():
        return ("python", "-m", "mypy", ".")
    if "pyright" in tool:
        return ("pyright",)
    return None


def _python_requires(pyproject: PyProject) -> str | None:
    project_requires = pyproject.get("project", {}).get("requires-python")
    if project_requires:
        return str(project_requires)
    poetry_deps = pyproject.get("tool", {}).get("poetry", {}).get("dependencies", {})
    python_constraint = poetry_deps.get("python")
    return str(python_constraint) if python_constraint else None
