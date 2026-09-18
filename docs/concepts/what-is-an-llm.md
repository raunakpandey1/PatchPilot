# What a language model actually is

## In one sentence

A large language model is a function that takes text and returns a guess at
what text comes next — nothing more, and every property that follows from that
is the reason agents are hard to build.

## The problem it solves

You want a program that can read a bug report, look at some code, and say "the
bug is that line 40 forgets to check whether the table exists."

You cannot write that with `if` statements. The space of bug reports is
infinite and they are written in English. A language model is currently the only
tool that reads ambiguous natural language and produces a relevant answer.

That is what it is *for*. The rest of this page is what it is *not*.

## How it works, step by step

### 1. Text becomes tokens

A model does not see characters or words. It sees **tokens** — chunks of text,
roughly 3–4 characters each.

```
"sqlite_utils.Database(memory=True)"
  →  ["s", "qlite", "_utils", ".Database", "(memory", "=True", ")"]
```

Tokens matter for three practical reasons:

- **You pay per token** (or, on a free tier, you are *rationed* per token).
- **There is a maximum**, called the context window — the total tokens of
  input plus output the model can handle at once.
- **Code tokenises badly.** Prose is about 0.75 tokens per word; code, full of
  punctuation and unusual identifiers, is much denser. A 500-line Python file
  can be 6,000+ tokens.

That last point is the whole reason [RAG](rag-end-to-end.md) exists. You cannot
put a repository in a prompt. You have to choose what goes in.

### 2. It predicts the next token, repeatedly

Given the tokens so far, the model produces a probability for every possible
next token, picks one, appends it, and repeats.

That is the entire mechanism. Everything else — reasoning, code, explanations —
is a consequence of doing that extremely well over a very large amount of text.

### 3. Temperature decides how it picks

- `temperature=0` — always take the most likely token. As close to
  deterministic as you get.
- `temperature=1` — sample proportionally. More varied, less predictable.

PatchPilot uses **0 everywhere**. We are not looking for creative output; we
want the same input to produce the same patch, because otherwise no benchmark
number means anything.

## Three consequences that shape this entire project

**It does not know anything after its training cutoff, and it has never seen
your repository.** Everything it says about `sqlite-utils` is either recalled
from public training data — possibly a version years old — or invented. This is
why the code must be *retrieved and put in the prompt*, not asked about.

**It cannot tell you when it does not know.** The mechanism produces a
plausible next token regardless. A confident, well-formatted, completely wrong
root-cause analysis costs exactly as much as a correct one and looks identical.
This is the single most important fact about building on top of one. It is why
PatchPilot runs the tests.

**It has no memory between calls.** Each request is independent. Any "memory"
is text you resend. That is what [agent state](langgraph-state-nodes-edges.md)
is for.

## In PatchPilot

- [`llm/base.py`](../../src/patchpilot/llm/base.py) — the interface: messages
  in, text or a validated object out, with token counts attached.
- [`llm/gemini.py`](../../src/patchpilot/llm/gemini.py) — the real provider.
- [`llm/fake.py`](../../src/patchpilot/llm/fake.py) — a scripted one, used by
  every unit test.

Every call returns a `Usage` with input tokens, output tokens and latency,
because Phase 10 has to answer "where did the time and the money go?" and
retrofitting that means touching every call site.

## What goes wrong

**Trusting fluency.** The output is always well-formed. Well-formed and correct
are unrelated properties.

**Forgetting the context window.** Paste a large file into a prompt and the
model silently truncates or the request fails. Count tokens, do not guess.

**Using a model for lookup.** If the answer is in a file, read the file. See
[ADR-006](../adr/ADR-006-deterministic-detection.md).

**Non-zero temperature in a pipeline you want to measure.** Two runs give two
answers and you can no longer tell whether a change helped.

## Interview questions

**Q: What is a token and why should you care?**

The unit a model actually processes — roughly 3–4 characters. It matters because
billing, rate limits and the context window are all counted in tokens, and
because code tokenises much more densely than prose. A 500-line Python file can
exceed 6,000 tokens, which is why you cannot simply put a repository in a prompt.

**Q: Why does an LLM hallucinate?**

Because generating a plausible next token is the whole mechanism; there is no
separate step that checks whether the claim is true, and no internal signal for
"I don't know". The output is equally fluent whether or not it is right. You
handle it architecturally — ground the model in retrieved context, constrain its
output to a schema, and verify the result against something real. PatchPilot's
answer to hallucination is that it runs the repository's test suite.

**Q: Why temperature 0?**

Reproducibility. A benchmark is only meaningful if the same input gives the same
output; otherwise you cannot tell whether a change to retrieval improved things
or the model simply sampled differently that run.

## Official documentation

- [Gemini API — Text generation](https://ai.google.dev/gemini-api/docs/text-generation) — the actual request/response shape.
- [Gemini API — Tokens](https://ai.google.dev/gemini-api/docs/tokens) — counting tokens before you send.
