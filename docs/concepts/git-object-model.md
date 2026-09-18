# What git actually stores, and why cloning has options

## In one sentence

Git is a database of immutable snapshots, and cloning normally downloads every
version of every file that ever existed — which is usually far more than you
need.

## The problem it solves

You want to work on someone's code. You need the files. You probably also need
the history, because "when did this break?" is often the fastest route to "why
is this broken?"

But a repository that is ten years old contains ten years of file versions,
nearly all of which nobody will ever look at again. Downloading all of them
costs time and disk you may not have. Git gives you ways to take less — each
with something you give up.

## How it works, step by step

### The four kinds of object

Git stores everything as one of four object types, each identified by a hash of
its own contents:

| Object | Is | Analogy |
|---|---|---|
| **blob** | a file's contents (no name) | a photograph |
| **tree** | a directory: names pointing at blobs and other trees | the album page listing which photo goes where |
| **commit** | one tree + author + message + parent commit(s) | a dated note saying "this is what the album looked like on Tuesday" |
| **tag** | a name pinned to a commit | a sticky label saying "v1.0" |

Two things follow from this.

**Identical content is stored once.** A blob is named by the hash of its
contents, so a file that never changes across 500 commits is one blob, not 500.

**History is a chain.** Each commit points at its parent. Walking that chain
backwards is what `git log` does. This is why a commit's hash cannot be changed
after the fact — changing anything changes the hash, which changes every
descendant's hash.

### What a clone actually transfers

`git clone` negotiates with the server about which objects you are missing, and
the server sends them in a compressed bundle called a *packfile*.

By default, that means **all commits, all trees, and all blobs, for all
history** — including every old version of every file.

### The three strategies

```
FULL          git clone <url>
              everything. Slow, large, complete.

SHALLOW       git clone --depth=1 <url>
              one commit and its files.
              ⚠️  There is no history. git log shows one entry.
                  git blame cannot tell you who changed a line.

BLOBLESS      git clone --filter=blob:none <url>
              all commits and trees, file contents fetched on demand.
              Full history. Old file versions downloaded only if you ask.
```

**Blobless is the interesting one.** You get the complete commit graph — so
`log`, `blame`, and "which commit introduced this" all work — but you skip the
bulk, which is historical file contents. If you later ask for an old version,
git quietly fetches it then.

### What we measured

Real numbers from [metrics.md](../metrics.md), on `pallets/flask`:

| strategy | size | commits readable |
|---|---:|---:|
| full | 14.5 MB | 200 |
| shallow | 2.6 MB | **1** |
| blobless | 6.5 MB | 200 |

Shallow is the smallest and it is useless to us — one commit of history means
Phase 4 cannot ask when a bug appeared. Blobless gives the same history as a
full clone at 45% of the size.

## In PatchPilot

- [`tools/git.py`](../../src/patchpilot/tools/git.py) — `CloneStrategy` and the
  `clone()` function. We use **blobless**.
- `GitRepository.files_changed_in(sha)` — which files a commit touched. This is
  how Phase 3 gets free training labels: for a closed issue, the commit that
  fixed it names exactly the files that needed to change.
- [`scripts/benchmark_clone.py`](../../scripts/benchmark_clone.py) — the
  measurement above.

## The other half: issues are not in git

Worth saying loudly, because it surprises people:

**A git repository contains code and history. It does not contain issues, pull
requests, comments, labels, or reviews.** Those live in GitHub's database and
are reachable only through [the API](rest-apis-and-rate-limits.md).

This is why PatchPilot talks to two entirely separate systems about the same
project, and why they fail in different ways: git can be slow or huge; the API
can rate-limit you.

## What goes wrong

**Choosing shallow because it is fastest.** It is, and it silently removes the
ability to ask historical questions. You will not notice until three phases
later when `git blame` returns nothing useful.

**Recursing submodules.** `.gitmodules` is a file in the repository — written by
whoever controls the repository — that says "also fetch code from this other
URL." Following it means fetching from wherever a stranger points. PatchPilot
never recurses submodules.

**Letting git prompt.** Clone a URL you cannot access and git will block
forever waiting for a username at a terminal nobody is watching. We set
`GIT_TERMINAL_PROMPT=0` and point `GIT_ASKPASS` at `false`.

**Letting git read your config.** By default git reads `~/.gitconfig`, including
your **credential helper**. Cloning a hostile URL could hand over stored
credentials. We set `GIT_CONFIG_NOSYSTEM=1` and a throwaway `HOME` —
`_hardened_env` in [tools/git.py](../../src/patchpilot/tools/git.py).

**Walking the directory instead of asking git.** `os.walk()` finds `.git`
internals, build output and untracked junk. `git ls-files` returns exactly what
git tracks.

## Interview questions

**Q: You clone with `--depth=1`. What did you just lose?**

History. `git log` shows one commit, `git blame` cannot attribute a line to the
commit that wrote it, and "which change introduced this bug" becomes
unanswerable. For a build system that only needs current files that is a fine
trade; for anything doing root-cause analysis it is disqualifying.

**Q: What is a blobless clone and when would you use one?**

`--filter=blob:none` downloads all commits and trees but no file contents up
front, fetching blobs on demand. You use it when you need the full commit graph
but not every historical version of every file — which is most tooling. We
measured 45% of a full clone's size on flask with identical history access.

**Q: Name two ways cloning an attacker-controlled repository can hurt you, even
though cloning does not execute code.**

Submodules — `.gitmodules` can point at arbitrary URLs, so recursing means
fetching code from wherever the attacker says. And size — a repository can
contain a 40 GB file or millions of small ones, filling your disk or hanging the
clone. Both are handled by refusing submodule recursion and enforcing time and
size budgets. A third is symlinks pointing outside the tree, which is why every
filesystem access goes through [the workspace](untrusted-input.md).

**Q: Where do GitHub issues live?**

Not in the repository. In GitHub's database, reachable only over the API. Any
design that only clones can never see them.

## Official documentation

- [`git clone`](https://git-scm.com/docs/git-clone) — read `--depth`, `--filter`, `--single-branch`, `--recurse-submodules`.
- [Git Internals — Git Objects](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects) — blobs, trees and commits from first principles.
- [`git rev-list` — object filtering](https://git-scm.com/docs/git-rev-list#Documentation/git-rev-list.txt---filterltfilter-specgt) — what `blob:none` means precisely.
