# Chunking code (and why prose advice does not apply)

## In one sentence

Split code on its own structure — function and class boundaries — because a
function cut in half produces two pieces that are individually useless.

## The problem it solves

You cannot put a repository in a prompt, so retrieval returns pieces. The
question is where to cut.

The standard advice, which you will find in every RAG tutorial, is: **1000
characters with 200 characters of overlap**. For prose that is tolerable —
paragraphs are roughly interchangeable and a sentence split across two chunks is
usually recoverable.

For code it is actively harmful:

```python
    def rows_where(self, where=None, args=None):
        sql = f"select * from [{self.name}]"
        if where is not None:
─────────────────────── chunk boundary ───────────────────────
            sql += f" where {where}"
        return self.db.execute(sql, args or [])
```

Now consider what each half is worth:

- **The first chunk** has the function name and an incomplete condition. It will
  match a search for `rows_where` — and it does not contain the bug.
- **The second chunk** has the logic and no name. A search for `rows_where`
  will never find it.

The information is still there, and retrieval can no longer get at it. That is
worse than either half being missing, because it *looks* like a hit.

## How it works, step by step

### 1. Parse instead of counting characters

Python ships a parser. `ast.parse()` gives a tree where every function and class
knows its own start and end line. So the boundaries are not guessed — they are
read off the source.

```python
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        chunk = lines[node.lineno - 1 : node.end_lineno]
```

### 2. Classes: whole, or split into methods

A small class stays whole — splitting a 20-line class loses the relationship
between its methods for no benefit.

A large one is split, because `sqlite-utils`' `Table` class is over a thousand
lines. As one chunk it would be truncated by the embedding model (512 tokens),
and a query about one method would retrieve the entire class, most of which is
irrelevant.

So an oversized class becomes:

- one **summary chunk** — signature and docstring, so a search for the class
  itself hits something meaningful rather than an arbitrary method
- one chunk **per method**, each carrying the class name

A bug lives in a method, so a method is the unit of retrieval.

### 3. Every chunk gets a context header

```
# sqlite_utils/db.py · class Table · method rows_where
def rows_where(self, where=None, args=None):
    ...
```

This is not decoration. A retrieved function body is often ambiguous alone —
`def get(self, key)` appears in a dozen files. The header restores the context a
human gets from looking at the file, and because it is part of the embedded
text, it also improves the match.

### 4. Fall back, deliberately, when structure runs out

- **A function larger than the ceiling** is split into overlapping windows. The
  overlap is the mitigation: a boundary cannot separate a condition from its
  body in *every* window.
- **Markdown** splits on headings — documentation's equivalent of a function
  boundary.
- **Unparseable Python** falls back to blank-line splitting. A repository is
  untrusted input, and "this file does not compile" is a normal state of a real
  codebase, not a reason to lose the file.
- **Unknown languages** split on blank lines, a weak signal but a better one
  than a character count, because people use blank lines to separate logical
  units.

### 5. Chunk ids make indexing idempotent

The id is a hash of repository, path, line range and content. Re-indexing an
unchanged file produces the same ids, so upserting overwrites rather than
duplicating — which means a crashed indexing run can simply be re-run.

The corollary: if a function *moves*, its line numbers change, so it becomes a
new chunk and the old one lingers. That is why re-indexing clears the
repository's chunks first.

## In PatchPilot

[`rag/chunking.py`](../../src/patchpilot/rag/chunking.py). Real output on this
repository's own ranking module:

```
class      Factor            ranking.py:69-81     489 chars
class      RankedIssue       ranking.py:84-119   1283 chars
function   rank_issue        ranking.py:122-150  1187 chars
function   _find_blockers    ranking.py:170-190   846 chars
```

Every boundary is a declaration boundary.

## What goes wrong

**Chunks bigger than the embedding model's limit.** BGE-small takes 512 tokens
and silently truncates the rest. A 3000-character chunk is partly invisible, and
nothing tells you.

**Chunks too small.** A three-line helper alone carries almost no signal, and
thousands of them dilute the index.

**Dropping the context header.** The single cheapest quality improvement in this
module, and the easiest to leave out.

**Indexing generated files.** A minified bundle is one line of 200,000
characters. It will chunk, and every chunk is noise.

**Assuming everything is parseable.** Real repositories contain files that do
not compile, files in languages you did not plan for, and files with broken
encodings. All three must produce *something* rather than an exception.

## Interview questions

**Q: How do you chunk code, and why not fixed-size chunks?**

Parse it and cut on declaration boundaries — functions, classes, methods — using
Python's `ast` module. Fixed-size chunking splits functions in half, and both
halves are worse than useless: one has the name and no logic, so it matches
searches it cannot answer, and the other has the logic and no name, so it is
unfindable. Code has structure that tells you where the seams are; prose does
not, which is why the standard advice does not transfer.

**Q: What do you do with a class that is bigger than a chunk?**

Split it into a summary chunk — signature and docstring — plus one chunk per
method, each tagged with the class name. A thousand-line class as a single chunk
would exceed the embedding model's token limit and be silently truncated, and a
query about one method would retrieve the whole thing. Bugs live in methods, so
methods are the retrieval unit.

**Q: What is the context header for?**

A retrieved function body is often ambiguous — `def get(self, key)` could be
anywhere. Prefixing the file path, class and symbol restores the context a human
would have from opening the file, and since it is embedded along with the body,
it also makes the match better. It is the cheapest quality improvement in the
chunker.

**Q: What happens when a file will not parse?**

Fall back to blank-line splitting. A repository is untrusted input and real
codebases contain files that do not compile, so losing a file entirely is the
wrong failure. There is a test for exactly that case.

## Official documentation

- [Python `ast`](https://docs.python.org/3/library/ast.html) — `parse`, node types, `lineno`/`end_lineno`.
- [Qdrant — Vector search basics](https://qdrant.tech/documentation/concepts/search/) — why chunk size interacts with the embedding model's limit.
