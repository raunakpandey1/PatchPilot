# ADR-008 — Abstract the LLM provider behind a Protocol

**Status:** Accepted · **Phase:** 2 · **Date:** 2026-09-18

## Context

PatchPilot has no budget, so development runs on Google's free Gemini tier. Two
other models are nevertheless required:

- **Tests** need a model that is free, instant, and returns the same thing every
  time. No real provider is any of those.
- **The Phase 11 benchmark** may be worth running once through a stronger paid
  model, to separate "the architecture is wrong" from "the free model is weak".

Three models, one codebase.

## Options considered

1. **Call the Gemini SDK directly from nodes.** Simplest; couples every node to
   one vendor and makes offline tests impossible without mocking the SDK.
2. **Use LangChain's chat model interface.** Already supports many providers,
   and integrates with LangGraph. Brings a large dependency surface and its own
   abstractions into business logic.
3. **A small Protocol of our own, with adapters per provider.**
4. **A gateway** (LiteLLM, OpenRouter) that normalises providers behind one API.
   Another dependency and, for paid routes, another account.

## Decision

Option 3: a `Protocol` with exactly two methods — `complete` and
`complete_structured` — and one factory function that is the only place a
concrete provider is named.

## Reasoning

The requirement is real and present, not speculative: the second implementation
already exists and runs on every test. That is the difference between this and
premature abstraction.

**Why not LangChain's interface.** It would work, and it would put a large
third-party abstraction in the middle of the domain logic. The stated
architectural rule for this project is that business logic does not import
LangChain; keeping the model interface ours is what makes that rule true rather
than aspirational. The surface we need is small enough that adapting it
ourselves is a few hundred lines.

**Why a Protocol rather than a base class.** Structural typing: an
implementation needs the methods, not an inheritance relationship. The fake
provider is not coupled to the real one, and a future provider could be written
without importing anything from this package.

**Why usage accounting is in the interface.** Every call returns tokens and
latency. Phase 10 reports cost per node, and adding that later would mean
touching every call site.

## Tradeoffs

**Against:**

- **Lowest common denominator.** Provider-specific features are unavailable
  unless the interface grows. Mitigated by needing only two capabilities — text
  and schema-constrained output — that every serious provider supports.
- **Adapters to maintain.** Each provider's quirks (Gemini calls the assistant
  role `model`) are ours to handle.
- **Swapping is not free in practice.** Prompts tuned for one model are not
  automatically good on another, and benchmark numbers do not transfer. The
  abstraction saves the plumbing, not the evaluation.

**For:** offline, free, deterministic tests; a one-line provider switch; failure
modes (unavailable vs. invalid response) expressed in our vocabulary rather than
a vendor's exception hierarchy.

## Consequences

- Exactly one function — `build_provider` — names a concrete provider class.
  Anything else importing one is a review failure.
- Provider errors are mapped into `LLMUnavailable` / `LLMResponseInvalid` at the
  adapter boundary.
- Every benchmark result records `provider.name`, because a metric without the
  model that produced it is meaningless.

## Interview questions

**Q: Why not just call the Gemini SDK directly?**

Because the test suite needs a model that is free, deterministic and offline,
and no real provider is. With the abstraction, 121 tests run in seconds against
a scripted implementation that satisfies the same protocol. The provider swap is
a secondary benefit; the primary one is that the tests exist at all.

**Q: Is this not premature abstraction?**

It would be with one implementation. There are two in use today and the second
is exercised on every test run. The test I apply is whether the alternative
implementation exists now — not whether I can imagine needing one.

**Q: What breaks when you actually swap providers?**

Prompt quality and benchmark comparability. The plumbing is a config change; the
results are not transferable, because a prompt tuned for one model is not
automatically good on another. That is why the provider name is recorded
alongside every measurement.

**Q: Why `Protocol` over `ABC`?**

Structural rather than nominal typing — an implementation just needs the right
methods. The fake provider does not inherit from the real one, and a third-party
provider could be written with no import from this package. Two tests assert
both implementations satisfy the protocol, so nothing is lost by not inheriting.

## Behavioural question this answers

> *"Tell me about a constraint that improved your design."*

Having no budget forced a decision I might otherwise have got wrong. I could not
call a real model from the test suite — no money, no determinism, no offline
runs — so I put a two-method protocol in front of the provider and wrote a
scripted implementation for tests. That turned out to be the more valuable half:
the fake can replay fixed sequences, repeat an answer forever to test that a
debug loop eventually gives up, and let a test assert on what the model was
actually shown. The constraint produced a better-tested system than a budget
would have.
