"""Chunking tests.

The property that matters: **a chunk boundary must not fall inside a
declaration**. A function split in half gives you two useless pieces — one with
a signature and no body, one with a body and no name.
"""

from __future__ import annotations

from patchpilot.rag.chunking import (
    MAX_CHUNK_CHARS,
    Chunk,
    ChunkKind,
    chunk_file,
)

MODULE = '''
"""A small module for exercising the chunker."""
import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT = 30
CONSTANT = 42


def add(a, b):
    """Add two numbers."""
    return a + b


class Table:
    """A database table."""

    def __init__(self, name):
        self.name = name

    def rows_where(self, where=None):
        if not self.exists():
            raise NoTable(self.name)
        return self.db.execute(f"select * from {self.name} where {where}")
'''


def chunks_for(source: str = MODULE, path: str = "db.py", **kwargs) -> list[Chunk]:
    return chunk_file("acme/widget", path, source, **kwargs)


# --- the core property ------------------------------------------------------


def test_functions_are_not_split_in_half():
    """The whole reason for AST chunking. A fixed-size splitter would cut
    `rows_where` between its condition and its body, leaving neither half
    useful. Whichever chunk contains the signature must contain the body.

    Note the class here is small, so it stays whole and `rows_where` lives
    inside the `Table` chunk — see test_small_classes_stay_whole.
    """
    chunks = chunks_for()
    containing = [c for c in chunks if "def rows_where" in c.text]

    assert len(containing) == 1, "the declaration must not straddle a boundary"
    assert "raise NoTable" in containing[0].text
    assert "self.db.execute" in containing[0].text


def test_top_level_functions_and_classes_become_chunks():
    symbols = {c.symbol for c in chunks_for()}

    assert "add" in symbols
    assert "Table" in symbols


def test_chunks_record_their_location():
    """Retrieved context is only useful if you can point at where it came from."""
    add = next(c for c in chunks_for() if c.symbol == "add")

    assert add.file_path == "db.py"
    assert add.start_line < add.end_line
    assert ":" in add.location


def test_module_level_code_is_not_lost():
    """Imports and constants are small but they are where a module's
    dependencies are declared — often exactly what an investigation needs."""
    module_chunks = [c for c in chunks_for() if c.kind is ChunkKind.MODULE]

    assert module_chunks
    assert "import os" in module_chunks[0].text
    assert "CONSTANT = 42" in module_chunks[0].text


# --- context headers --------------------------------------------------------


def test_header_carries_file_and_symbol():
    """A bare `def get(self, key)` could be from any file. The header restores
    the context a human would have from looking at the file."""
    add = next(c for c in chunks_for() if c.symbol == "add")

    assert "db.py" in add.header()
    assert "add" in add.header()
    assert add.embedding_text().startswith(add.header())


def test_methods_keep_their_class_name():
    big_class = "class Huge:\n    " + "\n    ".join(
        f"def method_{i}(self):\n        return {i} * " + "x" * 200 for i in range(12)
    )
    chunks = chunk_file("acme/widget", "huge.py", big_class)
    methods = [c for c in chunks if c.kind is ChunkKind.METHOD]

    assert methods, "an oversized class should be split into methods"
    assert all(m.parent_symbol == "Huge" for m in methods)
    assert methods[0].qualified_symbol.startswith("Huge.")


# --- size handling ----------------------------------------------------------


def test_oversized_functions_are_windowed_with_overlap():
    """When structure runs out, fall back to windows — but overlap them, so a
    boundary cannot separate a condition from its body in every window."""
    body = "\n".join(f"    line_{i} = compute({i})" for i in range(400))
    chunks = chunk_file("acme/widget", "big.py", f"def enormous():\n{body}\n")

    assert len(chunks) > 1
    assert all(len(c.text) <= MAX_CHUNK_CHARS * 1.5 for c in chunks)
    assert all(c.symbol == "enormous" for c in chunks)

    # Overlap: consecutive windows share lines.
    first, second = chunks[0], chunks[1]
    assert second.start_line < first.end_line


def test_small_classes_stay_whole():
    """Splitting a 20-line class into methods would lose the relationship
    between them for no benefit."""
    chunks = chunks_for()
    table = [c for c in chunks if c.symbol == "Table"]

    assert len(table) == 1
    assert table[0].kind is ChunkKind.CLASS
    assert "rows_where" in table[0].text


# --- robustness -------------------------------------------------------------


def test_unparseable_python_falls_back_instead_of_crashing():
    """A repository is untrusted input. 'It does not compile' must not stop
    the indexer — it is a normal state of a real codebase."""
    broken = "def oops(:\n    this is not python\n" + "x = 1\n" * 40

    chunks = chunk_file("acme/widget", "broken.py", broken)

    assert chunks, "a broken file should still produce something retrievable"


def test_empty_file_produces_nothing():
    assert chunk_file("acme/widget", "empty.py", "   \n\n  ") == []


# --- other file types -------------------------------------------------------


def test_markdown_splits_on_headings():
    """Headings are documentation's equivalent of a function boundary."""
    doc = (
        "# Title\n\n" + "Intro paragraph. " * 10 +
        "\n\n## Installation\n\n" + "Run pip install. " * 10 +
        "\n\n## Usage\n\n" + "Call the function. " * 10
    )

    chunks = chunk_file("acme/widget", "README.md", doc)

    assert len(chunks) >= 3
    assert all(c.kind is ChunkKind.DOCUMENTATION for c in chunks)
    assert {c.symbol for c in chunks} >= {"Title", "Installation", "Usage"}


def test_config_files_are_kept_whole():
    chunks = chunk_file("acme/widget", "pyproject.toml", "[tool.pytest]\ntestpaths = ['tests']\n" * 3)

    assert all(c.kind is ChunkKind.CONFIG for c in chunks)


def test_unknown_languages_fall_back_to_blank_line_splitting():
    source = "\n\n".join(f"fn block_{i}() {{ {'x' * 100} }}" for i in range(30))

    chunks = chunk_file("acme/widget", "main.rs", source)

    assert chunks


# --- identity ---------------------------------------------------------------


def test_chunk_ids_are_stable_across_runs():
    """Ids come from location plus content, which makes indexing idempotent:
    re-indexing an unchanged file overwrites rather than duplicating."""
    first = [c.id for c in chunks_for()]
    second = [c.id for c in chunks_for()]

    assert first == second


def test_changing_content_changes_the_id():
    original = chunks_for()
    edited = chunks_for(MODULE.replace("return a + b", "return a - b"))

    assert {c.id for c in original} != {c.id for c in edited}


def test_test_files_are_flagged_not_dropped():
    """Flagged rather than excluded: 'where is this bug' wants production code,
    but 'how should this behave' is often answered best by the test."""
    chunks = chunk_file("acme/widget", "tests/test_db.py", MODULE, is_test=True)

    assert chunks
    assert all(c.is_test for c in chunks)
