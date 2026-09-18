"""Ingestion tests — what gets indexed, and what deliberately does not."""

from __future__ import annotations

import subprocess

import pytest

from patchpilot.analysis.repository import analyze_repository
from patchpilot.models import Repository
from patchpilot.rag.embeddings import HashingEmbeddings
from patchpilot.rag.ingestion import (
    MAX_FILE_BYTES,
    chunk_repository,
    index_repository,
    should_index,
)
from patchpilot.rag.store import VectorStore
from patchpilot.tools.git import GitRepository

REPOSITORY = Repository(
    full_name="acme/widget", owner="acme", name="widget", default_branch="main",
    clone_url="https://github.com/acme/widget.git", html_url="https://github.com/acme/widget",
)

GIT_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
    "GIT_CONFIG_NOSYSTEM": "1",
}


# --- selection --------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "size", "expected"),
    [
        ("src/db.py", 5_000, True),
        ("README.md", 3_000, True),
        ("pyproject.toml", 1_000, True),
        ("poetry.lock", 200_000, False),          # thousands of hashes, matches nothing
        ("package-lock.json", 90_000, False),
        ("node_modules/lib/a.js", 500, False),    # someone else's dependency
        ("vendor/thing/x.py", 500, False),
        ("logo.png", 5_000, False),               # not text
        ("dist/bundle.js", 900, False),
        ("huge_generated.py", MAX_FILE_BYTES + 1, False),
    ],
)
def test_file_selection(path, size, expected):
    """Every irrelevant chunk is a chance to return the wrong thing. Selection
    is part of retrieval quality, not a preprocessing detail."""
    assert should_index(path, size) is expected


# --- indexing a real repository ---------------------------------------------


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()

    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths=['tests']\n")
    (root / "src" / "db.py").write_text(
        "class Table:\n"
        "    def rows_where(self, where):\n"
        "        return self.db.execute(where)\n"
    )
    (root / "src" / "render.py").write_text("def render(t):\n    return t.upper()\n")
    (root / "tests" / "test_db.py").write_text("def test_rows():\n    assert True\n")
    (root / "README.md").write_text("# widget\n\n" + "Documentation paragraph. " * 20)
    (root / "poetry.lock").write_text("hash = 'abc'\n" * 500)
    # A generated file large enough to trip the size budget. Unlike poetry.lock
    # this one *is* classified as source by the analyzer, so it exercises the
    # ingestion-level skip rather than being filtered upstream.
    (root / "src" / "generated_pb2.py").write_text("X = 1\n" * (MAX_FILE_BYTES // 6 + 100))

    env = {**GIT_ENV, "HOME": str(tmp_path)}
    for args in (["git", "init", "-b", "main"], ["git", "add", "."], ["git", "commit", "-m", "x"]):
        subprocess.run(args, cwd=root, env=env, check=True, capture_output=True)

    return analyze_repository(REPOSITORY, GitRepository(root))


def test_chunk_repository_covers_source_docs_and_config(snapshot):
    chunks, indexed, skipped = chunk_repository(snapshot)

    paths = {c.file_path for c in chunks}
    assert "src/db.py" in paths
    assert "README.md" in paths
    assert indexed >= 3


def test_lock_files_never_reach_the_index(snapshot):
    """Two layers exclude them, which is the point of checking here.

    The repository analyzer does not classify `.lock` as source, docs or config,
    so it never reaches ingestion at all. `should_index` rejects it as well, as
    defence in depth for any caller that bypasses the snapshot.
    """
    chunks, _, _ = chunk_repository(snapshot)

    assert "poetry.lock" not in {c.file_path for c in chunks}
    assert should_index("poetry.lock", 200_000) is False


def test_oversized_generated_files_are_skipped(snapshot):
    """A 500 KB single-purpose generated file is real source by classification
    and useless to retrieve. Chunking it would flood the index with noise."""
    chunks, _, skipped = chunk_repository(snapshot)

    assert "src/generated_pb2.py" not in {c.file_path for c in chunks}
    assert skipped >= 1


def test_test_files_are_indexed_but_flagged(snapshot):
    """Flagged rather than dropped: 'where is this bug' wants production code,
    but 'how should this behave' is often best answered by the test."""
    chunks, _, _ = chunk_repository(snapshot)

    test_chunks = [c for c in chunks if c.file_path.startswith("tests/")]
    assert test_chunks
    assert all(c.is_test for c in test_chunks)
    assert all(not c.is_test for c in chunks if c.file_path.startswith("src/"))


def test_index_repository_reports_what_it_did(snapshot):
    embeddings = HashingEmbeddings(dimension=128)
    store = VectorStore(dimension=embeddings.dimension)

    result = index_repository(snapshot, store, embeddings)

    assert result.chunks_stored == store.count()
    assert result.files_indexed > 0
    assert result.files_skipped > 0
    assert result.embedding_model == embeddings.name
    assert "chunks from" in result.summary()


def test_reindexing_replaces_rather_than_accumulating(snapshot):
    """Without `replace`, chunks from a previous commit linger — and because
    chunk ids include line numbers, a shifted function becomes a *new* chunk,
    leaving a stale copy to be retrieved later."""
    embeddings = HashingEmbeddings(dimension=128)
    store = VectorStore(dimension=embeddings.dimension)

    first = index_repository(snapshot, store, embeddings)
    second = index_repository(snapshot, store, embeddings)

    assert store.count() == first.chunks_stored == second.chunks_stored


def test_indexing_an_empty_repository_is_not_an_error(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "README.md").write_text("x")
    env = {**GIT_ENV, "HOME": str(tmp_path)}
    for args in (["git", "init", "-b", "main"], ["git", "add", "."], ["git", "commit", "-m", "x"]):
        subprocess.run(args, cwd=root, env=env, check=True, capture_output=True)

    empty_snapshot = analyze_repository(REPOSITORY, GitRepository(root))
    embeddings = HashingEmbeddings(dimension=64)

    result = index_repository(empty_snapshot, VectorStore(dimension=64), embeddings)

    assert result.chunks_stored == 0
