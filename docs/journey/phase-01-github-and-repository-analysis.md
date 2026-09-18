# Phase 1 — GitHub and repository analysis

**Goal:** given a repository name, get the code, get the issues, and describe
the project — deterministically, with no LLM anywhere.
**Date:** 2026-09-17

## 1. What we built

```
"simonw/sqlite-utils"
        │
        ├─── GitHubClient ──────► Repository metadata, Issues
        │    (REST + ETag cache + pagination + retries)
        │
        └─── clone (blobless) ──► GitRepository
                                   │  log / blame / diff / files_changed_in
                                   ▼
                          analyze_repository
                                   │
                                   ▼
                          RepositorySnapshot
             (languages, package manager, test command, file classification)
```

| Module | Responsibility |
|---|---|
| [`models.py`](../../src/patchpilot/models.py) | the vocabulary: `Repository`, `Issue`, `Commit`, `RepositorySnapshot` |
| [`errors.py`](../../src/patchpilot/errors.py) | typed failures, so callers can tell "wait" from "give up" |
| [`tools/github.py`](../../src/patchpilot/tools/github.py) | the API client |
| [`tools/http_cache.py`](../../src/patchpilot/tools/http_cache.py) | ETag storage |
| [`tools/git.py`](../../src/patchpilot/tools/git.py) | clone strategies, history reading, hardened environment |
| [`tools/workspace.py`](../../src/patchpilot/tools/workspace.py) | the directory nothing escapes |
| [`analysis/repository.py`](../../src/patchpilot/analysis/repository.py) | deterministic project characterisation |

## 2. How it works

**Two systems, not one.** Issues are not in the git repository — they live in
GitHub's database. So PatchPilot talks to git for code and history, and to the
REST API for everything else. They fail differently and are cached differently.
See [the git object model](../concepts/git-object-model.md).

**The API client is not a thin wrapper.** Four concerns are built in because
each is how the naive version fails: rate-limit tracking read from every
response, conditional requests so unchanged data costs nothing, `Link`-header
pagination so nothing is silently truncated, and a retry policy that retries
timeouts and 5xx but never a 404 or an exhausted budget. See
[REST APIs and rate limits](../concepts/rest-apis-and-rate-limits.md).

**Clones are blobless.** Full history, file contents on demand, roughly half the
disk of a full clone. See [ADR-004](../adr/ADR-004-blobless-clone.md).

**Analysis is pure lookup.** Every fact is read from a file that already
declares it. See [ADR-006](../adr/ADR-006-deterministic-detection.md).

**The repository is untrusted.** Every path goes through a workspace that
resolves symlinks and refuses escapes; git runs with a stripped environment so
it cannot read your credential helper; clones are bounded in time and size;
commands are argument lists, never strings. See
[untrusted input](../concepts/untrusted-input.md).

## 3. Why this way

Phase 1 is the input to everything. RAG indexes this clone. The sandbox runs
this test command. The benchmark pins this commit SHA. An error here does not
stay here — it surfaces three phases later disguised as something else, and gets
laundered into confident LLM prose along the way.

That is also why there is no LLM in this phase at all. Deterministic layers have
to be trustworthy before probabilistic ones sit on top of them.

## 4. Alternatives considered

| Decision | Alternatives | ADR |
|---|---|---|
| Blobless clone | full, shallow, treeless, API tarball | [004](../adr/ADR-004-blobless-clone.md) |
| REST + ETag | GraphQL, REST without caching | [005](../adr/ADR-005-rest-etag-over-graphql.md) |
| Deterministic analysis | LLM, LLM fallback | [006](../adr/ADR-006-deterministic-detection.md) |
| `subprocess` for git | GitPython, pygit2 | below |

**On `subprocess` versus GitPython** (a deviation from the plan). GitPython was
planned; `subprocess` was used. Three reasons: `subprocess.run(timeout=...)`
kills a hung clone, which is the primary defence against a hostile repository
and is awkward through a wrapper; the exact flags matter
(`--filter=blob:none`, `-c protocol.file.allow=never`) and passing them through
a library is more work than calling git; and the commands in the file are the
commands you would type, so the code teaches git rather than a library's opinion
about git.

## 5. Tradeoffs

- **Blobless is slower** — measured 20–35% — and not fully usable offline.
- **REST over-fetches.** ~80 fields per issue, we keep 12. Bandwidth is not the
  scarce resource; rate limit is.
- **Deterministic analysis only knows patterns we coded.** Unusual projects get
  `UNKNOWN`, and the agent then refuses work it cannot validate. That is the
  intended behaviour, not a gap.
- **`subprocess` means parsing text.** Handled by using control characters as
  field separators, with a test using a commit subject full of commas, pipes,
  quotes and backslashes.

## 6. Problems encountered

Three, all real, all in [failures.md](../failures.md).

**F-001 — an empty cache silently disabled itself.** `self.cache = cache or
NullCache()`. `MemoryCache` defines `__len__`, so an empty cache is falsy, and
`or` replaced it with a no-op. Nothing errored; it just used 5,000 requests an
hour instead of almost none.

**F-002 — a warm cache silently truncated paginated results.** The important
one. See below.

**F-003 — our own security control blocked our own tests.** `protocol.file.allow
=never` also blocks cloning from a local fixture repository. Fixed with an
explicit opt-in parameter rather than a weaker default.

## 7. Debugging process — F-002 in full

**Symptom.** Not a test failure. The benchmark printed:

```
run           issues  seconds  requests   304s  budget spent
cold cache        77     1.19         3      0             2
warm cache        61     0.69         2      2             0
```

The caching worked perfectly — zero budget spent — and returned **61 issues
instead of 77**. No error, no warning.

**Investigation.** The warm run made one fewer HTTP request than the cold run.
That pointed at pagination rather than parsing: a parsing bug would drop items
within a page, not skip a whole request. GitHub paginates via a `Link` response
header. So: does a 304 carry one? A direct check against the real API:

```
200 response:  link present: True
304 response:  link present: False
```

**Root cause.** GitHub omits `Link` from 304 responses. The client read the
next-page URL from the live response, so with a warm cache there was no link,
pagination stopped after page one, and page one's 100 raw items became 61 issues
after pull requests were filtered out.

**Fix.** Store the `Link` header in the cache alongside the payload
(`CachedResponse.link`) and restore it when serving a 304.

**Verification.** Both runs now return 77, warm still at zero budget. A unit
test, `test_pagination_survives_a_warm_cache`, reproduces it with a mock
transport that omits `Link` from its 304 — exactly like the real API.

**Why the tests missed it.** There were tests for ETag caching and tests for
pagination. Both passed. The bug lived in their interaction, and appeared only
when cold and warm results were compared *against each other* on real data. An
optimisation is not finished when it is fast; it is finished when it has been
shown to return the same answer.

## 8. Tests

| suite | count | runtime | needs |
|---|---:|---:|---|
| unit | 69 | ~2 s | nothing |
| integration | 6 | ~6 s | network, GitHub token |

Worth calling out:

- **`test_workspace.py`** — five real directory-escape techniques, including a
  symlink escape that defeats a string-prefix check. These are security tests.
- **`test_git.py`** builds a real repository rather than mocking git, including
  a commit subject containing commas, pipes, quotes and a backslash.
- **`test_analysis.py`** includes `src/latest/thing.py` must not be a test file
  — substring matching on "test" is the obvious implementation and the wrong
  one.
- **`test_github.py::test_pagination_survives_a_warm_cache`** — the F-002
  regression.

## 9. Metrics

Full detail in [metrics.md](../metrics.md). The two headline findings:

**A shallow clone reads 1 commit of history; blobless reads 200 at ~45% of a
full clone's size.** The fastest option was the one that could not do the job.

**A repeat issue fetch costs 0 rate-limit budget with conditional requests, and
2 without.** That is what makes a free tier viable.

## 10. Official documentation

- [GitHub — Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) — what counts against the budget, and what does not.
- [GitHub — Best practices for using the REST API](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api) — conditional requests, secondary limits.
- [`git clone`](https://git-scm.com/docs/git-clone) — `--depth`, `--filter`, `--single-branch`, `--recurse-submodules`.
- [`subprocess` security considerations](https://docs.python.org/3/library/subprocess.html#security-considerations) — why commands are argument lists here.

## 11. Interview questions

1. Where do GitHub issues live, and what does that imply for a system that only
   clones?
2. You clone with `--depth=1`. Which capability did you just lose?
3. You have 5,000 requests an hour and 500 repositories to keep fresh. Design
   the strategy.
4. What is the difference between a primary and a secondary rate limit, and why
   does it change your retry policy?
5. How do you test code that depends on GitHub, and what can that approach not
   catch?
6. `git clone` does not execute code. Name three ways cloning a hostile
   repository can still hurt you.
7. Why is the test command a tuple rather than a string?
8. Why is there no LLM in this phase?

## 12. STAR story

Two, both real: **S-001** (the pagination bug) and **S-002** (the clone strategy
benchmark). See the [story bank](../interview/story-bank.md).
