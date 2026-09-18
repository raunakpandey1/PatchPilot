# ADR-019 — A model fallback chain and a response cache

**Status:** Accepted · **Phase:** 4 · **Date:** 2026-09-18

## Context

This project runs on a free Gemini tier. Two things happened on the first live
run, within minutes of each other:

1. The configured model, `gemini-2.0-flash`, returned **404** — retired, with the
   API naming a successor.
2. The replacement returned **503 UNAVAILABLE** for several minutes, while other
   models served normally:

```
gemini-3.8-flash        503
gemini-3.7-flash        503
gemini-3.6-flash        OK   (10.9 s)
gemini-3.5-flash-lite   OK   ( 1.4 s)
```

Later in the same session, `gemini-3.8-flash` returned **429 RESOURCE_EXHAUSTED**.

The retry policy behaved correctly — exponential backoff, four attempts, then a
clean halt with a reason. It did not help, because retrying one overloaded model
harder does not make it available.

## Options considered

1. **Retry harder** — more attempts, longer backoff.
2. **Fail the run** and tell the user to try later.
3. **A fallback chain** — try a different model.
4. **Queue and retry later** — persist the run, resume when capacity returns.

## Decision

**A fallback chain**, plus a **disk response cache** and a **hard call budget**.

## Reasoning

**Retrying harder is the wrong shape.** Capacity varies *per model*, minute to
minute. The useful question is not "wait longer" but "ask someone else".

**Queueing is correct at scale** and is what a production system would do. It
needs durable job storage and a worker, which is a larger change than the problem
warrants for one laptop.

### The distinction that makes fallback safe

Fall back on **unavailability**, never on a **bad response**:

| error | meaning | action |
|---|---|---|
| `LLMUnavailable` — 503, 429, network | the request was fine, the service could not serve it | **another model probably can** → fall back |
| `LLMResponseInvalid` — 400, schema violation | the request or prompt is wrong | **it will be wrong everywhere** → raise |

Getting this backwards is the classic failure of retry-and-fallback code: it
spends three models' quota collecting three copies of the same error, and hides
a real bug behind a slow one.

### Why a cache belongs in the same decision

Free tier means a hard daily call limit, and development means sending the same
prompt many times — re-run after a code change, re-run after a crash, re-run the
benchmark. Each repeat spends quota to obtain an answer already obtained.

The cache keys on **everything that can change the answer**: model identity,
system prompt, messages, schema, temperature, output limit. Leave any out and it
serves an answer to a different question, which is worse than no cache because it
is invisible.

It is only sound because every call is at `temperature=0`. At a higher
temperature the same request is *supposed* to vary, and caching would silently
remove the variation the caller asked for.

### And a budget, because loops have bugs

`CallBudget` counts calls that reach a provider — cache hits are free — and
raises when exhausted. It sits below the debug loop's own bounds precisely so a
bug in those bounds cannot spend a day's quota.

## Tradeoffs

**Against:**

- **Results become model-dependent.** A benchmark row produced by a fallback is
  not comparable with one from the primary. Handled by `provider.name` reporting
  the model that *answered*, and every measurement recording it.
- **A stale cache serves stale answers** after a prompt change. Mitigated by the
  prompt being part of the key, so an edited prompt is a different entry.
- **Three layers of provider wrapper** — cache over fallback over provider — is
  more indirection than one.

**For:** runs complete during degraded service; iterating on a fixed pipeline is
free; and no loop can exhaust the day's budget.

## Consequences

- **Cache outermost, fallback inside.** A cache hit must not consume budget and
  must not care which model originally answered.
- That ordering produced a real bug ([failures.md F-007](../failures.md)): the
  cache key used `inner.name`, and `FallbackProvider.name` reports whichever
  model last answered — so after any fallback the same request hashed differently
  and the cache never hit, silently. Fixed by resolving a stable identity once at
  construction.
- Model IDs are treated as unstable: `patchpilot models` lists what a key can
  actually reach, because guessing one from memory is how the 404 happened.

## Interview questions

**Q: Your model provider returned 503 for several minutes. What did you do?**

Checked whether it was the provider or that model, and it was that model — two
others served the same prompt normally in the same minute. So retrying harder was
the wrong shape of fix; the useful move was to ask a different model. I added a
fallback chain that tries models in order on unavailability.

**Q: What must a fallback chain *not* do?**

Fall back on a bad response. A 503 or a 429 means the request was fine and the
service could not serve it, so another model probably can. A 400 or a schema
violation means the request itself is wrong, and it will be equally wrong at the
next model — falling back there spends three models' quota to collect the same
error three times and hides a real bug behind a slow one.

**Q: How do you keep a free tier usable while developing?**

A disk cache keyed on everything that can change the answer — model, system
prompt, messages, schema, temperature, token limit — so a repeated prompt costs
nothing. It is only sound because every call is at temperature zero; at higher
temperatures caching would remove variation the caller asked for. Plus a hard
call budget one layer below the loops, so a bug in a loop bound cannot spend the
day.

**Q: What bug did that stack produce?**

The cache key included the provider's name, and the fallback chain reports
whichever model last answered — so after one fallback the same request hashed to
a different key and the cache stopped hitting entirely, silently. Two components
that were individually correct. Fixed by resolving a stable identity, the whole
chain, once at construction. It was only caught because one test exercised the
full stack rather than each layer alone.

## Behavioural question this answers

> *"Tell me about a time a system failed in production and how you responded."*

The first live end-to-end run failed: the model I had configured had been retired
and returned 404, and its replacement then returned 503 for several minutes. My
retry logic worked exactly as designed — backoff, four attempts, clean halt with
a reason — and was useless, because retrying one overloaded model harder does not
make it available. So I checked whether the provider or that model was
unavailable, found two other models serving the same prompt fine in the same
minute, and built a fallback chain. The part I was careful about was what *not*
to fall back on: a 400 or a schema violation will fail identically everywhere,
and falling back there would have turned a loud fixable error into a slow hidden
one.
