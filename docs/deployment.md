# Deploying PatchPilot

## The constraint that shapes everything

PatchPilot validates a patch by running the target repository's test suite in a
container with **no network access**. That requires privileged containers, and
no free application host grants them — not Hugging Face Spaces, not Render, not
Railway.

There are three honest responses to that, and only one of them is acceptable:

1. Run untrusted tests without a sandbox. **No.**
2. Deploy without validation and present patches as results. **No** — an
   unvalidated patch shown as a result is exactly what this project is built to
   avoid.
3. Move the execution somewhere that *can* sandbox.

## The split-plane architecture

```
 Hugging Face Space (free)              GitHub Actions (free for public repos)
 ┌──────────────────────────┐           ┌────────────────────────────────┐
 │ CONTROL PLANE            │           │ EXECUTION PLANE                │
 │                          │ dispatch  │                                │
 │ analyse repository       ├──────────►│ fresh VM per job               │
 │ rank issues              │           │ apply the patch                │
 │ search code              │◄──────────┤ run the tests                  │
 │ diagnose · propose patch │  result   │ report structured outcome      │
 │ policy · approval screen │           │                                │
 └──────────────────────────┘           └────────────────────────────────┘
       decides what to try                     runs untrusted code
```

This is not a workaround dressed up as a design. **Separating the component that
decides from the component that executes untrusted code is how you would build
this at any scale** — it is why CI systems have runners, and why the runner is
disposable. Here it also happens to be the only free option.

## What the Space can and cannot do

| Capability | On the Space | Why |
|---|---|---|
| Analyse a repository | ✅ | deterministic, reads config files |
| Rank issues with reasoning | ✅ | arithmetic, no model |
| Semantic code search | ✅ | embeddings run locally on CPU |
| Policy engine · injection scan | ✅ | ordinary code |
| Diagnose · propose a patch | ⚠️ | needs an API key in Space secrets |
| **Validate a patch** | ❌ | needs a privileged container → Actions |
| Commit · open a PR | ❌ | deliberately absent from a public demo |

Five of those need no API key at all, which is why the demo is useful with an
exhausted quota — and the deterministic half is the half that best shows the
engineering anyway.

## Deploying the Space

1. Create a Space at [huggingface.co/new-space](https://huggingface.co/new-space)
   — **Docker** SDK, free CPU tier.

2. Add this as a second remote and push:

   ```bash
   git remote add space https://huggingface.co/spaces/<user>/patchpilot
   git push space main
   ```

3. Put the Space README's YAML header in place (see `README_SPACE.md` in this
   repository) so Spaces knows the port and SDK.

4. Optional — add `PATCHPILOT_GEMINI_API_KEY` under **Settings → Variables and
   secrets** to enable the diagnosis tab. Without it, five tabs still work.

**Docker rather than the Gradio SDK** because this needs Python 3.13:
`tomllib` is standard library only from 3.11 and the repository analyzer uses
it. The Gradio SDK pins an older interpreter.

### What the image does at build time

- installs a **subset** of dependencies (`requirements-space.txt`) — no
  langgraph, no Docker SDK, no MCP server. Smaller image, faster cold start,
  less surface on a public host.
- copies the **pre-built retrieval index** for `simonw/sqlite-utils`. Embedding
  a repository takes several minutes on CPU, which is far too slow for a web
  request and too slow for a container start.
- **warms the embedding model** into the image, so a visitor is not waiting ~30 s
  for a download on the first search.

## Wiring up validation

The workflow is [`.github/workflows/validate-patch.yml`](../.github/workflows/validate-patch.yml).
It takes a repository, a ref, a diff and a test command; applies the patch;
runs the tests; and reports a structured result.

Two details worth noticing:

- **`persist-credentials: false`** — the job must not be able to push. It reads
  code and runs tests, and nothing else.
- **`git apply --check` before applying** — so "the patch does not apply" is
  distinguishable from "the tests failed". Those are different outcomes and the
  agent repairs them differently.

To dispatch it from the Space you need a fine-grained token with
`actions: write` on the repository that hosts the workflow, stored as a Space
secret. That token is the one genuinely sensitive thing in this deployment, and
it is scoped to triggering one workflow.

## Known limitations

**Free Spaces sleep after ~48 hours idle.** The first visit takes about 30
seconds to wake. The page says so, rather than letting a visitor conclude it is
broken.

**The shipped index is derived data.** ~9 MB, committed so the demo answers
instantly. It goes stale as `sqlite-utils` changes; rebuilding is
`patchpilot index simonw/sqlite-utils` and a rebuild of the image.

**Indexing new repositories is not offered on the Space.** Several minutes of
CPU per repository is not a web request, and a public endpoint that runs one is
an easy way to be rate-limited off the free tier.

**The Actions round trip is slow** — a minute or two per validation, against
about a second locally. Acceptable for a demo; it is the reason the local CLI
remains the real development path.
