# Not hard-coding your model provider

## In one sentence

Every part of PatchPilot talks to a small interface — messages in, text or a
validated object out — so the model behind it can be swapped by changing one
configuration value.

## The problem it solves

This is not abstraction for its own sake. There is a concrete reason, and it is
worth stating plainly because "make it pluggable" is usually a bad instinct.

This project has **no budget**. It is developed against a free Gemini tier. But:

- every unit test needs a model that is free, instant and gives the same answer
  every time — which no real provider is;
- the final benchmark may be worth running once through a stronger paid model,
  to separate "the architecture is wrong" from "the free model is weak";
- free tiers change.

Three different models, one codebase. That is the requirement.

## How it works, step by step

### 1. The interface says only what nodes actually need

```python
class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    def complete(self, messages, *, system=None, temperature=0.0,
                 max_output_tokens=4096) -> Completion: ...

    def complete_structured(self, messages, schema, *, ...) -> StructuredCompletion: ...
```

Note what is **missing**: no `safety_settings`, no `top_k`, no provider-shaped
config object. The moment a node reaches for one of those, the abstraction has
failed and swapping providers becomes a rewrite.

It is a `Protocol`, not a base class — implementations do not inherit from
anything, they simply have these methods. So the fake provider in the test suite
is not coupled to the real one.

### 2. Each provider adapts its own API

Gemini calls the assistant role `model`. That translation lives in
[`llm/gemini.py`](../../src/patchpilot/llm/gemini.py):

```python
role_map = {Role.USER: "user", Role.ASSISTANT: "model"}
```

No node knows this. That is the entire point of an adapter.

### 3. One place constructs a provider

```python
def build_provider(settings) -> LLMProvider:
    if settings.llm_provider == "fake":
        return FakeProvider()
    return GeminiProvider(api_key=..., model=settings.llm_model)
```

This is deliberately the only function in the codebase that names a concrete
provider class. If a node ever imports `GeminiProvider` directly, the
abstraction is defeated — and that is a greppable, reviewable rule.

### 4. Every call reports what it cost

```python
@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    latency_s: float
```

Built into the interface rather than added later, because Phase 10 has to answer
"where did the time and money go?" and retrofitting means touching every call
site. The agent state sums it with a reducer, so the cost of a whole run is one
field.

## Where abstractions like this usually go wrong

Worth knowing, because an interviewer may well push on it.

**Lowest common denominator.** If the interface only exposes what every provider
shares, you lose the features you are paying for. PatchPilot needs exactly two
things — text, and schema-constrained output — both of which every serious
provider supports, so the floor is not low.

**Leaky abstraction.** One node needs a provider-specific feature, and the
interface grows a `provider_options` dict. Now it is a union of every provider's
API with extra steps.

**Abstracting before you need to.** The common failure. Here the need is
concrete and present: the test suite *already* uses a second implementation. An
abstraction with only one implementation is usually a guess about the future.

## In PatchPilot

- [`llm/base.py`](../../src/patchpilot/llm/base.py) — the protocol and value types.
- [`llm/gemini.py`](../../src/patchpilot/llm/gemini.py) — the real provider,
  with retries on 429/5xx and not on 400.
- [`llm/fake.py`](../../src/patchpilot/llm/fake.py) — scripted, deterministic,
  and it records the prompts it was given.
- [`llm/__init__.py`](../../src/patchpilot/llm/__init__.py) — the factory.

The fake is not a lesser implementation. It is the one used by 121 tests, and it
does things the real one cannot: replay a fixed sequence, repeat the last answer
forever (which is how you test a debug loop that must eventually give up), and
let a test assert on what the model was actually shown.

## What goes wrong

**Provider-specific error handling leaking upward.** Gemini's `APIError` is
mapped to `LLMUnavailable` or `LLMResponseInvalid` inside the adapter. A node
catching `google.genai.errors.APIError` would break the moment the provider
changes.

**Forgetting that models differ in more than API shape.** A prompt tuned for one
model is not automatically good on another. The interface makes swapping
mechanical; it does not make the results identical. That is a real limit, and
the honest way to state it is that the abstraction saves you the *plumbing*, not
the *evaluation*.

**Retrying the wrong things.** A 429 is worth retrying; a 400 means the request
was malformed and will be malformed again.

## Interview questions

**Q: Why abstract the LLM provider?**

Because this project genuinely runs three: a free Gemini tier for development, a
scripted fake for the 121 unit tests, and possibly a stronger paid model for one
final benchmark. Without the abstraction the tests would need network, money and
determinism they cannot have. It is not speculative — the second implementation
exists and is used on every test run.

**Q: What is the risk of that abstraction?**

Lowest-common-denominator design, and leakage. I kept the surface to two
methods — text, and schema-constrained output — both universally supported, so I
am not giving up much. The rule that keeps it honest is that exactly one
function in the codebase names a concrete provider class; anything else
importing one is a review failure.

**Q: You swap Gemini for a different model. What actually still breaks?**

Prompts and results. The interface makes the plumbing a config change, but a
prompt tuned for one model is not automatically good on another, and the
benchmark numbers are not transferable. Which is exactly why the provider name
is recorded in every result — a metric without the model that produced it is
meaningless.

**Q: Why a Protocol rather than an abstract base class?**

Structural typing: an implementation just needs the methods, not an inheritance
relationship. The fake provider is not coupled to the real one, and a future
provider could be written without importing anything from this package. There is
a test asserting both satisfy the protocol, so the checking is not lost.

## Official documentation

- [PEP 544 — Protocols](https://peps.python.org/pep-0544/) — structural subtyping.
- [Gemini API — Quickstart](https://ai.google.dev/gemini-api/docs/quickstart) — the API being adapted.
