# ADR-005 — REST with ETag caching instead of GraphQL

**Status:** Accepted · **Phase:** 1 · **Date:** 2026-09-17

## Context

PatchPilot needs issues, their labels, and eventually their comments. GitHub
offers two APIs:

- **REST** — one endpoint per resource, 5,000 requests/hour authenticated.
- **GraphQL** — one endpoint, you specify the fields you want, billed as a
  points budget rather than a request count.

The classic argument for GraphQL is the N+1 problem: fetching 100 issues and
then each one's comments is 101 REST calls and one GraphQL query.

## Options considered

1. **REST only, no caching.** Simple. Burns budget on unchanged data.
2. **REST with ETag conditional requests.** Unchanged data returns a 304 that
   does not count against the limit.
3. **GraphQL.** Fewer round trips, exactly the fields needed.
4. **Both** — GraphQL for bulk reads, REST for everything else.

## Decision

**REST with ETag conditional requests and `since=` incremental sync.**

## Reasoning

The N+1 argument is real but it is not our bottleneck. We fetch issue *lists*,
which REST returns 100 at a time — one request per hundred issues, not one per
issue. Comments are fetched only for the single issue being worked on.

Against that, three things favour REST here:

**Caching.** ETags work per URL on REST. GraphQL posts to a single endpoint with
the query in the body, so HTTP-level caching does not apply and you would build
your own. Measured, conditional requests took a repeat fetch from 2 requests to
**0** budget consumed ([metrics.md](../metrics.md)). On a free-tier project that
is the dominant consideration.

**Error semantics.** REST returns 404 for missing, 401 for bad credentials, 403
for rate limits — mapped directly to typed exceptions. GraphQL returns HTTP 200
with an `errors` array, so every error path is parsed from the body.

**Complexity budget.** GraphQL adds a query language, a schema, and a separate
cost model to a phase whose real lessons are rate limits, pagination and
retries.

## Tradeoffs

**Against:**
- Over-fetching. GitHub's issue JSON has ~80 fields; we keep about 12. Wasted
  bandwidth, though not wasted rate limit, which is what is scarce.
- If we later need comments for many issues at once, that becomes N+1 and
  GraphQL would win.

**For:** HTTP caching works; standard status codes; simpler to read and to test
with a mock transport.

## Consequences

- A cache layer is required, not optional — it is the reason this decision
  holds. See [`http_cache.py`](../../src/patchpilot/tools/http_cache.py).
- Cached entries must store the `Link` header as well as the body, because
  GitHub omits it on 304s. This was learned the hard way — see
  [failures.md](../failures.md) F-002.
- If Phase 4 needs comments for many issues in bulk, revisit: a GraphQL adapter
  behind the same interface is the migration path.

## Interview questions

**Q: REST or GraphQL for GitHub?**

Depends on what is scarce. GraphQL wins when you have an N+1 problem — many
per-item follow-up fetches. We mostly read issue *lists*, which REST returns 100
per request, so N+1 barely applies. What is scarce for us is rate-limit budget,
and REST's ETag conditional requests take a repeat fetch to zero cost, which
does not translate to GraphQL because it is a single POST endpoint. REST also
has real status codes, which makes error handling and retry policy cleaner.

**Q: When would you switch?**

If we needed comments or reviews for many issues at once. That is a genuine N+1
and GraphQL would collapse it into one request. The client is an adapter behind
a typed interface, so that is a new implementation rather than a rewrite.

**Q: Why can't you cache GraphQL the same way?**

HTTP caching is keyed on the URL and method. GraphQL is a POST to one endpoint
with the query in the body, so every request looks identical to an HTTP cache.
You can build application-level caching keyed on the query, but you are writing
what REST gives you for free — and GitHub's "304 does not count against your
limit" behaviour is specific to conditional GETs.

## Behavioural question this answers

> *"Tell me about a time you went against the conventional recommendation."*

The standard advice for GitHub's API is GraphQL, to avoid N+1 fetches. I chose
REST, because the constraint that actually binds this project is rate-limit
budget on a free tier, and REST supports ETag conditional requests where
unchanged data returns a 304 that GitHub does not charge for. I measured it: a
repeat fetch went from 2 requests to 0 budget consumed. GraphQL's advantage is
real but it addresses a problem I do not have, and it would have cost me the one
mechanism that does address the problem I do have.
