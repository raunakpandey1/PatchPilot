# ADR-013 — Chunk code on AST boundaries, not character counts

**Status:** Accepted · **Phase:** 3 · **Date:** 2026-09-18

## Context

Retrieval returns pieces of a repository, so something has to decide where the
pieces begin and end. The standard RAG recipe is fixed-size chunks — commonly
1000 characters with 200 characters of overlap.

## Options considered

1. **Fixed-size character windows with overlap.** The default everywhere.
2. **Line-based windows.** Same idea, slightly more code-aware.
3. **AST-based chunking** — parse the file, cut on declarations.
4. **Whole files as chunks.** Maximum context, no boundary problem.
5. **tree-sitter**, for AST chunking across many languages.

## Decision

**AST-based chunking for Python** using the standard library's `ast`, with
structure-aware fallbacks: headings for Markdown, blank lines for unknown or
unparseable files, and overlapping windows only for declarations that exceed the
size ceiling.

## Reasoning

**Fixed-size chunking breaks code in a specific, damaging way.** A function cut
in half produces two pieces that are individually useless: one has the signature
and no logic, so it matches searches it cannot answer; the other has the logic
and no name, so it is unfindable. The information is present and retrieval can
no longer reach it — worse than either half being absent, because it looks like
a hit.

**Code has structure that tells you where the seams are.** Prose does not, which
is why the standard advice exists and why it does not transfer. Python ships a
parser; the boundaries do not have to be guessed.

**Whole files fail on both ends.** `sqlite-utils`' main module is thousands of
lines — far beyond the embedding model's 512-token limit, so most of it would be
silently truncated. And retrieving a whole file to answer a question about one
method fills the prompt with noise.

**tree-sitter is the right answer for multi-language support** and is deferred,
not rejected. It is a compiled dependency and a second grammar-loading mechanism,
for a project that currently targets one Python repository. The chunker
dispatches on file type, so adding it later is a new branch rather than a
redesign.

## The design that follows

- **Small class → one chunk.** Splitting a 20-line class loses the relationship
  between its methods for no gain.
- **Large class → summary chunk plus one chunk per method.** A thousand-line
  class exceeds the embedding limit, and a query about one method should not
  retrieve the whole thing. Bugs live in methods.
- **Context header on every chunk** —
  `# sqlite_utils/db.py · class Table · method rows_where`. A retrieved
  `def get(self, key)` is ambiguous alone; the header restores the context a
  human gets from opening the file, and since it is embedded with the body, it
  improves the match too. Cheapest quality gain in the module.
- **Chunk ids hash location and content**, so re-indexing an unchanged file
  writes identical ids and overwrites. Indexing is idempotent and a crashed run
  can simply be re-run.

## Tradeoffs

**Against:**

- **Python only, properly.** Everything else gets a weaker fallback.
- **Variable chunk sizes.** A 5-line helper and a 200-line function are both one
  chunk. Uneven, and it reflects the code rather than fighting it.
- **Parsing cost**, though `ast.parse` is milliseconds.
- **Moved code becomes new chunks.** Ids include line numbers, so a function
  shifted down by an edit is a new chunk and the old one lingers — which is why
  re-indexing clears the repository first. A content-only hash would avoid it
  and would collide across identical helpers.

**For:** no chunk boundary falls inside a declaration; every chunk has a symbol,
a file and a line range, so retrieved context can be cited; metadata comes free
from the parse.

## Consequences

- `ChunkKind` (function / class / method / module / documentation / config)
  becomes a filter dimension in retrieval.
- Chunk metadata carries `is_test`, so tests are indexed but separable — "where
  is this bug" wants production code, "how should this behave" is often best
  answered by the test.
- Unparseable files must still produce chunks. A repository is untrusted input,
  and "does not compile" is a normal state of a real codebase.

## Interview questions

**Q: How do you chunk code, and why not the standard fixed-size approach?**

Parse it and cut on declaration boundaries. Fixed-size splitting cuts functions
in half, and both halves are worse than useless — one has the name without the
logic and matches searches it cannot answer, the other has the logic without the
name and is unfindable. Prose survives that treatment because paragraphs are
roughly interchangeable; code does not.

**Q: What do you do with a class too large for one chunk?**

Split it into a signature-and-docstring summary plus one chunk per method, each
tagged with the class name. Otherwise a thousand-line class exceeds the
embedding model's token limit and is silently truncated, and a query about one
method retrieves all of it.

**Q: What is the header for?**

Disambiguation. `def get(self, key)` could be from any file, so each chunk is
prefixed with its path, class and symbol. It is part of the embedded text, so it
improves the match as well as making retrieved context citable. It is the
cheapest quality improvement in the chunker and the easiest to omit.

**Q: Why not tree-sitter?**

It is the right answer for multi-language support and I deferred it rather than
rejected it. It is a compiled dependency and a second grammar mechanism, for a
project targeting one Python repository. The chunker dispatches on file type, so
adding it is a new branch rather than a redesign.

## Behavioural question this answers

> *"Tell me about a time you ignored a standard recommendation."*

Every RAG tutorial says chunk at 1000 characters with 200 of overlap, and for
code that is actively harmful — it cuts functions in half, leaving one piece
with a name and no logic and another with logic and no name. I used Python's AST
to cut on declaration boundaries instead. The reasoning is that the standard
advice is written for prose, where paragraphs are interchangeable, and code has
explicit structure telling you where the seams are. I kept the fixed-size
approach as a fallback for oversized functions and unparseable files, with
overlap, because "never use it" would have been the same mistake in the opposite
direction.
