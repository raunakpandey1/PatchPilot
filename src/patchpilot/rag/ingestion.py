"""Indexing a repository: files → chunks → vectors → store.

Do not blindly embed every file
-------------------------------
A repository contains things that are actively harmful to retrieve:

* **lock files** — ``poetry.lock`` is thousands of lines of hashes that match
  nothing meaningful and dilute the index
* **minified and generated code** — one line, thousands of characters
* **binaries and images** — not text
* **vendored dependencies** — someone else's code, usually not the bug

Every irrelevant chunk is a chance to return the wrong thing, plus time and disk
spent. Selection is part of retrieval quality, not a preprocessing detail.

What is indexed, and why tests are kept
---------------------------------------
Source, documentation, and configuration. Tests are indexed but **flagged**
(``is_test=True``) rather than excluded, because the right answer depends on the
question: "where is this bug?" wants production code, but "how is this function
expected to behave?" is often answered best by its test. Excluding them would
throw that away; the flag lets the caller decide per query.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel

from patchpilot.logging import get_logger
from patchpilot.models import RepositorySnapshot
from patchpilot.rag.chunking import Chunk, chunk_file
from patchpilot.rag.embeddings import EmbeddingModel
from patchpilot.rag.store import VectorStore

log = get_logger("rag.ingestion")

# Files whose content is real but useless to retrieve.
SKIP_NAMES = frozenset({"poetry.lock", "package-lock.json", "yarn.lock", "Pipfile.lock", "uv.lock"})
SKIP_DIRECTORIES = frozenset({".git", "node_modules", "vendor", "dist", "build", ".venv", "__pycache__"})
INDEXABLE_SUFFIXES = frozenset({
    ".py", ".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yaml", ".yml",
    ".js", ".ts", ".go", ".rs", ".java", ".rb",
})

# A file larger than this is generated, minified, or data. Real source is
# smaller; chunking a 2 MB single-line file produces noise.
MAX_FILE_BYTES = 500_000

# Embedding in batches: one call per chunk wastes most of the model's
# throughput, and one call for everything would hold the whole corpus in memory.
EMBED_BATCH = 64


class IngestionResult(BaseModel):
    """What an indexing run did. Reported, and used by the benchmark."""

    repository: str
    files_considered: int
    files_indexed: int
    files_skipped: int
    chunks_created: int
    chunks_stored: int
    duration_s: float
    embedding_model: str

    def summary(self) -> str:
        return (
            f"{self.repository}: {self.chunks_stored} chunks from {self.files_indexed} files "
            f"({self.files_skipped} skipped) in {self.duration_s:.1f}s using {self.embedding_model}"
        )


def should_index(file_path: str, size_bytes: int) -> bool:
    """Decide whether a file earns a place in the index."""
    path = Path(file_path)

    if path.name in SKIP_NAMES:
        return False
    if SKIP_DIRECTORIES & set(path.parts):
        return False
    if path.suffix.lower() not in INDEXABLE_SUFFIXES:
        return False
    return size_bytes <= MAX_FILE_BYTES


def chunk_repository(snapshot: RepositorySnapshot) -> tuple[list[Chunk], int, int]:
    """Read a cloned repository and produce chunks.

    Returns the chunks plus counts of files indexed and skipped, so the caller
    can report what was left out rather than silently dropping it.
    """
    root = snapshot.local_path
    test_files = set(snapshot.test_files)
    all_files = (
        list(snapshot.source_files)
        + list(snapshot.test_files)
        + list(snapshot.doc_files)
        + list(snapshot.config_files)
    )

    chunks: list[Chunk] = []
    indexed = skipped = 0

    for relative in all_files:
        absolute = root / relative
        try:
            size = absolute.stat().st_size
        except OSError:
            skipped += 1
            continue

        if not should_index(relative, size):
            skipped += 1
            continue

        try:
            source = absolute.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped += 1
            continue

        produced = chunk_file(
            snapshot.repository.full_name, relative, source, is_test=relative in test_files
        )
        if produced:
            chunks.extend(produced)
            indexed += 1
        else:
            skipped += 1

    return chunks, indexed, skipped


def index_repository(
    snapshot: RepositorySnapshot,
    store: VectorStore,
    embeddings: EmbeddingModel,
    *,
    replace: bool = True,
) -> IngestionResult:
    """Index a repository into the vector store.

    ``replace=True`` clears the repository's existing chunks first. Without it,
    chunks from a previous commit linger — and because chunk ids include line
    numbers, a shifted function becomes a *new* chunk rather than an updated
    one, leaving a stale copy behind to be retrieved later.
    """
    started = time.monotonic()
    full_name = snapshot.repository.full_name

    if replace:
        store.delete_repository(full_name)

    chunks, indexed, skipped = chunk_repository(snapshot)
    stored = _embed_and_store(chunks, store, embeddings)

    result = IngestionResult(
        repository=full_name,
        files_considered=indexed + skipped,
        files_indexed=indexed,
        files_skipped=skipped,
        chunks_created=len(chunks),
        chunks_stored=stored,
        duration_s=round(time.monotonic() - started, 2),
        embedding_model=embeddings.name,
    )
    log.info("repository_indexed", **result.model_dump(exclude={"repository"}), repo=full_name)
    return result


def _embed_and_store(
    chunks: Sequence[Chunk], store: VectorStore, embeddings: EmbeddingModel
) -> int:
    stored = 0
    for batch in _batched(chunks, EMBED_BATCH):
        vectors = embeddings.embed_documents([c.embedding_text() for c in batch])
        stored += store.upsert(batch, vectors)
    return stored


def _batched(items: Sequence[Chunk], size: int) -> Iterable[Sequence[Chunk]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
