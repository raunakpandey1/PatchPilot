"""Splitting a repository into retrievable pieces.

Why chunking exists at all
--------------------------
You cannot put a repository in a prompt. `sqlite-utils` is about 100 files; the
main module alone is thousands of lines. A model has a context window measured
in tokens, and code tokenises densely — a 500-line Python file can exceed 6,000
tokens.

So retrieval has to return *pieces*. The question is where to cut.

Why not fixed-size chunks
-------------------------
The standard answer is "every 1000 characters, with 200 characters of overlap".
For prose that is tolerable. For code it is actively harmful::

    def rows_where(self, where=None, args=None):
        sql = f"select * from [{self.name}]"
        if where is not None:
    ─────────────────────── chunk boundary ───────────────────────
            sql += f" where {where}"
        return self.db.execute(sql, args or [])

Now neither half is useful. The first is a function with no body; the second is
a fragment with no name, so a search for "rows_where" will never match the part
that contains the bug.

Code has structure that prose does not, and that structure tells you exactly
where the seams are: a function is a unit of meaning, so it should be a unit of
retrieval.

What this module does
---------------------
Parse Python with the standard library's ``ast`` and cut on declarations —
functions, classes, methods. Everything else (Markdown, config, unknown
languages) falls back to structure-aware splitting on headings or blank lines.

Each chunk carries a **context header**::

    # sqlite_utils/db.py · class Table · method rows_where

A retrieved function body is often ambiguous on its own — ``def get(self, key)``
appears in a dozen files. The header restores the context a human would have
from looking at the file, and it is part of the embedded text, so it also
improves the match.
"""

from __future__ import annotations

import ast
import hashlib
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from patchpilot.logging import get_logger

log = get_logger("rag.chunking")

# BGE-small accepts 512 tokens. Code runs roughly 3 characters per token, so
# ~1800 characters is a safe ceiling that rarely truncates.
MAX_CHUNK_CHARS = 1800

# Below this, a chunk is usually a one-line helper or a stray import block. It
# is kept only if it is a whole declaration; otherwise it is merged.
MIN_CHUNK_CHARS = 60

# When a single function exceeds the ceiling it is split into windows. The
# overlap means a boundary cannot fall between a condition and its body without
# one of the windows containing both.
WINDOW_OVERLAP_LINES = 8


class ChunkKind(StrEnum):
    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    MODULE = "module"
    DOCUMENTATION = "documentation"
    CONFIG = "config"
    UNKNOWN = "unknown"


class Chunk(BaseModel):
    """One retrievable piece of a repository."""

    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    repository: str
    file_path: str
    start_line: int
    end_line: int
    kind: ChunkKind = ChunkKind.UNKNOWN
    symbol: str | None = None
    parent_symbol: str | None = None
    language: str = "unknown"
    is_test: bool = False

    @property
    def location(self) -> str:
        return f"{self.file_path}:{self.start_line}-{self.end_line}"

    @property
    def qualified_symbol(self) -> str:
        """``Table.rows_where`` rather than just ``rows_where``."""
        if self.parent_symbol and self.symbol:
            return f"{self.parent_symbol}.{self.symbol}"
        return self.symbol or Path(self.file_path).stem

    def header(self) -> str:
        """The context line prepended to the embedded text."""
        parts = [self.file_path]
        if self.parent_symbol:
            parts.append(f"class {self.parent_symbol}")
        if self.symbol:
            parts.append(f"{self.kind} {self.symbol}")
        return "# " + " · ".join(parts)

    def embedding_text(self) -> str:
        """What actually gets embedded: header plus body.

        The header matters more than it looks. A bare ``def get(self, key)``
        could be from any file; prefixed with its path and class, it is
        distinguishable — both to the model and to the person reading the
        retrieved context.
        """
        return f"{self.header()}\n{self.text}"


def chunk_file(
    repository: str,
    file_path: str,
    source: str,
    *,
    is_test: bool = False,
) -> list[Chunk]:
    """Split one file into chunks, choosing a strategy by file type."""
    if not source.strip():
        return []

    suffix = Path(file_path).suffix.lower()

    if suffix == ".py":
        return _chunk_python(repository, file_path, source, is_test=is_test)
    if suffix in {".md", ".rst", ".txt"}:
        return _chunk_markdown(repository, file_path, source)
    if suffix in {".toml", ".cfg", ".ini", ".yaml", ".yml", ".json"}:
        return _chunk_whole_file(
            repository, file_path, source, ChunkKind.CONFIG, language=suffix.lstrip(".")
        )
    return _chunk_by_blank_lines(repository, file_path, source)


# --- Python -----------------------------------------------------------------


def _chunk_python(
    repository: str, file_path: str, source: str, *, is_test: bool
) -> list[Chunk]:
    """Cut Python on declaration boundaries, using the AST.

    If the file does not parse — which happens, repositories contain broken
    files — fall back to blank-line splitting rather than losing the file
    entirely. A repository is untrusted input, and "it did not compile" is not a
    reason to crash the indexer.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        log.debug("python_parse_failed", file=file_path)
        return _chunk_by_blank_lines(repository, file_path, source)

    lines = source.splitlines()
    chunks: list[Chunk] = []
    covered: set[int] = set()

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            chunks.extend(_chunk_class(repository, file_path, lines, node, is_test=is_test))
            covered.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            chunks.extend(
                _chunk_declaration(
                    repository, file_path, lines, node,
                    kind=ChunkKind.FUNCTION, parent=None, is_test=is_test,
                )
            )
            covered.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))

    # Whatever is left — imports, constants, module docstring, top-level code.
    # It is small but it is where a module's dependencies are declared, which is
    # often exactly what an investigation needs.
    remainder = [
        (number, text)
        for number, text in enumerate(lines, start=1)
        if number not in covered and text.strip()
    ]
    if remainder and sum(len(t) for _, t in remainder) >= MIN_CHUNK_CHARS:
        chunks.append(
            _make_chunk(
                repository, file_path,
                text="\n".join(t for _, t in remainder),
                start_line=remainder[0][0], end_line=remainder[-1][0],
                kind=ChunkKind.MODULE, symbol=None, parent=None,
                language="python", is_test=is_test,
            )
        )

    return chunks


def _chunk_class(
    repository: str, file_path: str, lines: list[str], node: ast.ClassDef, *, is_test: bool
) -> list[Chunk]:
    """A class becomes either one chunk, or a summary plus one chunk per method.

    The split matters. `sqlite_utils`' ``Table`` class is well over a thousand
    lines; as a single chunk it would be truncated by the embedding model, and
    a query about one method would retrieve the entire class. Methods are the
    unit a bug lives in, so methods are the unit of retrieval — with the class
    name preserved on each so the context is not lost.
    """
    start, end = node.lineno, node.end_lineno or node.lineno
    body = "\n".join(lines[start - 1 : end])

    if len(body) <= MAX_CHUNK_CHARS:
        return [
            _make_chunk(
                repository, file_path, text=body, start_line=start, end_line=end,
                kind=ChunkKind.CLASS, symbol=node.name, parent=None,
                language="python", is_test=is_test,
            )
        ]

    chunks: list[Chunk] = []

    # A signature-and-docstring summary, so a search for the class itself hits
    # something meaningful rather than an arbitrary method.
    summary_end = _first_method_line(node) or min(start + 12, end)
    summary = "\n".join(lines[start - 1 : summary_end - 1]).strip()
    if len(summary) >= MIN_CHUNK_CHARS:
        chunks.append(
            _make_chunk(
                repository, file_path, text=summary, start_line=start,
                end_line=summary_end - 1, kind=ChunkKind.CLASS, symbol=node.name,
                parent=None, language="python", is_test=is_test,
            )
        )

    for child in node.body:
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            chunks.extend(
                _chunk_declaration(
                    repository, file_path, lines, child,
                    kind=ChunkKind.METHOD, parent=node.name, is_test=is_test,
                )
            )

    return chunks


def _chunk_declaration(
    repository: str,
    file_path: str,
    lines: list[str],
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    kind: ChunkKind,
    parent: str | None,
    is_test: bool,
) -> list[Chunk]:
    """One function or method. Split into overlapping windows only if oversized."""
    start, end = node.lineno, node.end_lineno or node.lineno
    body = "\n".join(lines[start - 1 : end])

    if len(body) <= MAX_CHUNK_CHARS:
        return [
            _make_chunk(
                repository, file_path, text=body, start_line=start, end_line=end,
                kind=kind, symbol=node.name, parent=parent,
                language="python", is_test=is_test,
            )
        ]

    return _split_oversized(
        repository, file_path, lines, start, end,
        kind=kind, symbol=node.name, parent=parent, is_test=is_test,
    )


def _split_oversized(
    repository: str,
    file_path: str,
    lines: list[str],
    start: int,
    end: int,
    *,
    kind: ChunkKind,
    symbol: str | None,
    parent: str | None,
    is_test: bool,
) -> list[Chunk]:
    """Window a too-large declaration, with overlap.

    This is the fixed-size strategy, used only where structure has run out. The
    overlap is the mitigation: a boundary cannot separate a condition from its
    body without one window containing both.
    """
    chunks: list[Chunk] = []
    window_lines = max(20, MAX_CHUNK_CHARS // 40)  # ~40 chars/line of code
    cursor = start

    while cursor <= end:
        stop = min(cursor + window_lines - 1, end)
        text = "\n".join(lines[cursor - 1 : stop])
        if text.strip():
            chunks.append(
                _make_chunk(
                    repository, file_path, text=text, start_line=cursor, end_line=stop,
                    kind=kind, symbol=symbol, parent=parent,
                    language="python", is_test=is_test,
                )
            )
        if stop >= end:
            break
        cursor = stop - WINDOW_OVERLAP_LINES + 1

    return chunks


def _first_method_line(node: ast.ClassDef) -> int | None:
    for child in node.body:
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            return child.lineno
    return None


# --- other file types -------------------------------------------------------

HEADING = re.compile(r"^#{1,6}\s+\S|^[=\-]{3,}$")


def _chunk_markdown(repository: str, file_path: str, source: str) -> list[Chunk]:
    """Split documentation on headings — its equivalent of a function boundary."""
    lines = source.splitlines()
    sections: list[tuple[int, list[str]]] = []
    current: list[str] = []
    current_start = 1

    for number, line in enumerate(lines, start=1):
        if HEADING.match(line) and current:
            sections.append((current_start, current))
            current, current_start = [line], number
        else:
            current.append(line)
    if current:
        sections.append((current_start, current))

    chunks: list[Chunk] = []
    for start, body_lines in sections:
        text = "\n".join(body_lines).strip()
        if len(text) < MIN_CHUNK_CHARS:
            continue
        for piece_start, piece in _hard_split(text, start):
            chunks.append(
                _make_chunk(
                    repository, file_path, text=piece, start_line=piece_start,
                    end_line=piece_start + piece.count("\n"),
                    kind=ChunkKind.DOCUMENTATION,
                    symbol=_heading_text(body_lines[0]), parent=None,
                    language="markdown", is_test=False,
                )
            )
    return chunks


def _heading_text(line: str) -> str | None:
    stripped = line.lstrip("#").strip()
    return stripped[:80] or None


def _chunk_whole_file(
    repository: str, file_path: str, source: str, kind: ChunkKind, *, language: str
) -> list[Chunk]:
    """Config files are small and only meaningful entire."""
    return [
        _make_chunk(
            repository, file_path, text=piece, start_line=start,
            end_line=start + piece.count("\n"), kind=kind,
            symbol=Path(file_path).name, parent=None, language=language, is_test=False,
        )
        for start, piece in _hard_split(source.strip(), 1)
    ]


def _chunk_by_blank_lines(repository: str, file_path: str, source: str) -> list[Chunk]:
    """Last resort for unknown languages and unparseable files.

    Blank lines are a weak signal, but they are a better guess than a fixed
    character count, because people use them to separate logical units.
    """
    lines = source.splitlines()
    chunks: list[Chunk] = []
    current: list[str] = []
    start = 1

    def flush(end: int) -> None:
        text = "\n".join(current).strip()
        if len(text) >= MIN_CHUNK_CHARS:
            chunks.append(
                _make_chunk(
                    repository, file_path, text=text, start_line=start, end_line=end,
                    kind=ChunkKind.UNKNOWN, symbol=None, parent=None,
                    language=Path(file_path).suffix.lstrip(".") or "unknown", is_test=False,
                )
            )

    for number, line in enumerate(lines, start=1):
        current.append(line)
        if len("\n".join(current)) >= MAX_CHUNK_CHARS and not line.strip():
            flush(number)
            current, start = [], number + 1

    if current:
        flush(len(lines))
    return chunks


def _hard_split(text: str, start_line: int) -> list[tuple[int, str]]:
    """Split on the character ceiling. Only used where structure gave out."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [(start_line, text)]

    pieces: list[tuple[int, str]] = []
    lines = text.splitlines()
    current: list[str] = []
    current_start = start_line

    for offset, line in enumerate(lines):
        current.append(line)
        if len("\n".join(current)) >= MAX_CHUNK_CHARS:
            pieces.append((current_start, "\n".join(current)))
            current = []
            current_start = start_line + offset + 1
    if current:
        pieces.append((current_start, "\n".join(current)))
    return pieces


def _make_chunk(
    repository: str,
    file_path: str,
    *,
    text: str,
    start_line: int,
    end_line: int,
    kind: ChunkKind,
    symbol: str | None,
    parent: str | None,
    language: str,
    is_test: bool,
) -> Chunk:
    # The id is derived from location and content, so re-indexing an unchanged
    # file produces identical ids — which makes indexing idempotent. Re-running
    # it overwrites rather than duplicating.
    digest = hashlib.sha256(
        f"{repository}:{file_path}:{start_line}:{end_line}:{text}".encode()
    ).hexdigest()[:32]

    return Chunk(
        id=digest, text=text, repository=repository, file_path=file_path,
        start_line=start_line, end_line=end_line, kind=kind, symbol=symbol,
        parent_symbol=parent, language=language, is_test=is_test,
    )
