# Structured output: making the model return objects, not prose

## In one sentence

Instead of asking for text and parsing it, you declare the shape you need and
the model is constrained to produce exactly that — so a node receives a
validated object or a clear error, never a paragraph it has to guess at.

## The problem it solves

A root-cause node needs three things: a file, a line, and an explanation.

Ask for text and you get:

```
Looking at the code, I believe the issue is in sqlite_utils/db.py around
line 40, where the `rows_where` method doesn't check whether the table exists...
```

Now write the parser. Find the filename — with a regular expression? Which
one? "around line 40" is not a number. Next week the model says "the problem
appears to be located in db.py (line 40-ish)" and your parser returns `None`
without raising, and a downstream node quietly proceeds with no file.

The failure is silent, which is the worst kind.

## How it works, step by step

### 1. Declare the shape

```python
class RootCause(BaseModel):
    file: str
    line: int
    explanation: str
```

### 2. Ask for it

```python
result = llm.complete_structured(
    [user("Why does this fail?\n\n" + context)],
    schema=RootCause,
)
result.value.file     # "sqlite_utils/db.py"   — a str, guaranteed
result.value.line     # 40                     — an int, guaranteed
```

### 3. What the provider actually does

This is the part worth understanding, because there are two very different
mechanisms and only one of them is reliable.

**Weak version — ask nicely.** Put "reply with JSON like {...}" in the prompt
and parse the reply. The model usually complies. Sometimes it wraps the JSON in
a code fence, or adds "Here's the analysis:" first, or omits a field.

**Strong version — constrain generation.** Gemini accepts a Pydantic model as
`response_schema` and restricts the tokens it is allowed to produce so the
output *must* fit the schema. The model is constrained while generating, not
corrected afterwards:

```python
config = types.GenerateContentConfig(
    response_mime_type="application/json",
    response_schema=schema,          # ← the Pydantic class itself
)
```

PatchPilot uses the strong version — see
[`llm/gemini.py`](../../src/patchpilot/llm/gemini.py).

### 4. Validation is still the last line of defence

Even constrained generation can fail, most commonly by **running out of output
tokens mid-JSON**. So the provider validates and, when validation fails,
distinguishes the causes:

```python
hint = " The response hit max_output_tokens, so the JSON is incomplete." if truncated else ""
raise LLMResponseInvalid(f"... did not match {schema.__name__}.{hint}")
```

That distinction matters because the fixes are opposite. Truncation means raise
`max_output_tokens`. A genuine schema mismatch means the prompt is
under-specified, and retrying identical input will not help.

## Why this matters more for an agent than for a chatbot

A chatbot's output goes to a human, who can cope with rewording. An agent's
output goes to **the next node**, which cannot.

Structured output converts a whole class of silent failures into loud ones. A
node given a validated `RootCause` either works, or the pipeline raised before
it ran. A node given a paragraph works most of the time and fails mysteriously
the rest.

It also makes the fake provider honest: `FakeProvider` can be scripted with a
schema-violating response, so the error path gets tested —
`test_structured_output_rejects_a_mismatched_response`.

## In PatchPilot

- [`llm/base.py`](../../src/patchpilot/llm/base.py) — `complete_structured`,
  generic over the schema type.
- [`llm/gemini.py`](../../src/patchpilot/llm/gemini.py) — the constrained
  implementation, with the truncation distinction.
- [`llm/fake.py`](../../src/patchpilot/llm/fake.py) — scripted schema objects
  and scripted failures.

From Phase 4 onward every model-powered node returns a schema: `RootCause`,
`FixPlan`, `PatchReview`. None of them parse prose.

## What goes wrong

**Schemas too large.** Twenty fields with long descriptions means more output
tokens, more chances to truncate, and worse quality per field. Ask for less.

**Optional fields everywhere.** `file: str | None` moves the problem downstream
— now the node has to handle `None` and usually forgets.

**Free text inside the schema.** `explanation: str` is unavoidable and fine; a
field called `code: str` expected to contain a whole valid patch is a schema
that validates while carrying garbage. Structure what you can check.

**Treating validation success as correctness.** A schema proves the *shape*. It
says nothing about whether line 40 is actually the bug. That is what the test
suite is for.

## Interview questions

**Q: How do you get reliable structured data out of a language model?**

Declare a schema and use the provider's constrained-generation support so the
model can only emit conforming tokens — rather than asking for JSON in the
prompt and parsing the reply. Then validate anyway, because constrained output
can still be cut off by the token limit. In this project that is
`complete_structured`, which takes a Pydantic class and returns a validated
instance or raises.

**Q: What is the difference between prompting for JSON and constrained
decoding?**

Prompting asks the model to behave and hopes; the model may add prose, wrap the
output in a code fence, or drop a field. Constrained decoding restricts which
tokens can be generated so the output must fit the schema. The first is a
request, the second is an invariant.

**Q: Your schema validated. Does that mean the answer is right?**

No. It means the answer has the right shape. `line: 40` is a valid integer
whether or not the bug is on line 40. Shape is checked by the schema;
correctness is checked by running the repository's tests. Conflating the two is
how you get a pipeline that is confidently wrong in a well-formed way.

**Q: What do you do when the model returns something invalid?**

Distinguish the causes. If the response was truncated, the fix is more output
tokens. If it genuinely did not match, the prompt is under-specified and
retrying the same input will produce the same failure — so the error message
says which happened. Blind retries on schema failures are a good way to spend a
quota achieving nothing.

## Official documentation

- [Gemini API — Structured output](https://ai.google.dev/gemini-api/docs/structured-output) — `response_schema` and its limits.
- [Pydantic — Models](https://docs.pydantic.dev/latest/concepts/models/) — validation, and what `ValidationError` tells you.
