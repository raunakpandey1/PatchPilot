# REST APIs, rate limits, and conditional requests

## In one sentence

A REST API is a website designed for programs instead of people, and a rate
limit is the number of times per hour it will answer you before it starts
saying no.

## The problem it solves

You want a list of a repository's open issues. You cannot get them from the
code — issues are not stored in the repository (see
[the git object model](git-object-model.md)). They live in GitHub's database,
and the only way to reach them is to ask GitHub's servers over the internet.

A REST API is that asking mechanism. You send an HTTP request to a URL, you get
back JSON.

```
GET https://api.github.com/repos/simonw/sqlite-utils/issues

[ {"number": 678, "title": "Crash on dotted table name", "state": "open", ...},
  {"number": 677, ...} ]
```

That is the whole idea. Everything below is about what goes wrong when you do
this at scale.

## How it works, step by step

### 1. Authentication — who are you?

Without a token, GitHub gives you **60 requests per hour**. With one, **5,000**.
That is an 83× difference for one HTTP header:

```
Authorization: Bearer ghp_xxxxxxxxxxxx
```

In PatchPilot this is set once, in the constructor, and never thought about
again — [tools/github.py](../../src/patchpilot/tools/github.py).

A token is a password. It goes in an environment variable, never in code, never
in git. See [configuration and secrets](configuration-and-secrets.md).

### 2. Rate limits — how many questions can you ask?

Every response tells you where you stand, in headers you get for free:

```
x-ratelimit-limit: 5000
x-ratelimit-remaining: 4873
x-ratelimit-reset: 1758134400     ← a Unix timestamp
```

PatchPilot reads these on **every** response, so it always knows the remaining
budget without spending a request to ask. That is `_record_rate_limit` in
[tools/github.py](../../src/patchpilot/tools/github.py).

There are actually **two** kinds of limit, and confusing them is a classic bug:

| | Primary limit | Secondary limit |
|---|---|---|
| **Means** | your hourly budget is spent | you are sending requests too fast |
| **Signal** | `403` with `x-ratelimit-remaining: 0` | `403`/`429` with budget remaining |
| **Right response** | stop; retrying cannot help | wait a moment, then retry |

PatchPilot raises `RateLimitExceeded` for the first (carrying the reset time, so
the caller can decide whether to wait) and retries the second with backoff.
Getting this backwards means either hammering a door that will not open for an
hour, or giving up on a request that would have succeeded in two seconds.

### 3. Pagination — what if there are 400 issues?

APIs do not return 400 items in one response. GitHub returns at most 100 and
tells you where the rest are, in a `Link` header:

```
Link: <https://api.github.com/repositories/140912432/issues?page=2>; rel="next",
      <https://api.github.com/repositories/140912432/issues?page=9>; rel="last"
```

The temptation is to write `for page in range(1, 10)`. Do not. You do not know
how many pages there are, and guessing means either missing data or wasting
requests on empty pages. **Follow the `next` link until there isn't one.** That
is `_paginate` in [tools/github.py](../../src/patchpilot/tools/github.py).

### 4. Conditional requests — the one that matters most

Here is the problem. You want to keep 500 repositories' issue lists fresh. Each
refresh costs a few requests. You have 5,000 per hour. The arithmetic does not
work.

But almost nothing changes between checks. So GitHub gives every response a
fingerprint, called an **ETag**:

```
ETag: W/"37b67900fbf0d946e8ea368e1fdb1cc4dd9313c7948a4bcde72b7fbd6b25c8a5"
```

Store it. Next time, send it back:

```
If-None-Match: W/"37b67900fbf..."
```

If nothing changed, GitHub replies:

```
304 Not Modified
```

— with no body, and **it does not count against your rate limit.**

You already have the data. You spent nothing to confirm it is still current.

Measured on the real API ([metrics.md](../metrics.md)): re-fetching this
repository's issues cost **2 requests** cold and **0** warm.

Think of it as asking *"has anything changed since version X?"* instead of
*"send me everything again."*

## In PatchPilot

- [`tools/github.py`](../../src/patchpilot/tools/github.py) — the client:
  auth, rate-limit reading, pagination, retries, conditional requests.
- [`tools/http_cache.py`](../../src/patchpilot/tools/http_cache.py) — where
  ETags and payloads are stored. `MemoryCache` for tests, `FileCache` for real
  runs.
- [`scripts/benchmark_github.py`](../../scripts/benchmark_github.py) — the
  script that measured the saving.

## What goes wrong

**Pull requests come back from the issues endpoint.** On GitHub, every pull
request *is* an issue underneath. `GET /issues` returns both. If you do not
filter them out, your agent will rank already-solved work as available. The
giveaway is a `pull_request` key in the JSON — see `Issue.is_pull_request` in
[models.py](../../src/patchpilot/models.py).

**Retrying things that cannot be fixed by retrying.** A 404 will still be a 404
on the fourth attempt. Retry timeouts and 5xx; never retry 404, 401, or an
exhausted budget.

**Caching the body but not the headers.** This bit us for real: GitHub omits the
`Link` header from 304 responses, so a warm cache stopped paginating and quietly
returned 21% fewer issues. See [failures.md](../failures.md) F-002.

**Assuming the clock.** `x-ratelimit-reset` is a Unix timestamp in UTC, not a
duration and not your local time.

## Interview questions

**Q: You have 5,000 requests an hour and want to track 500 repositories. How?**

Do not re-fetch things that have not changed. Three layers: store the ETag and
send `If-None-Match`, so unchanged data returns a free 304; use `since=` to ask
only for issues updated after your last sync; and poll active repositories more
often than quiet ones. Track your own remaining budget from the response headers
so you slow down before GitHub cuts you off, rather than after.

**Q: What's the difference between a 403 for rate limiting and a 429?**

GitHub uses both. What matters is not the code but whether
`x-ratelimit-remaining` is zero. Zero means the hourly budget is gone and
retrying is pointless until the reset. Non-zero means a secondary limit — you
are going too fast — and backing off then retrying will work.

**Q: Why not just loop over page numbers?**

You do not know how many pages exist, the count can change between requests, and
GitHub has moved to cursor-based pagination where page numbers are not
meaningful. The `Link` header is the server telling you the answer; guessing
gets you silent truncation or wasted requests.

**Q: How would you know if your caching was actually working?**

Count the 304s and measure the rate-limit budget consumed before and after an
identical repeat request. If the budget does not move, the cache is working. We
measured exactly this, and the measurement is what caught a bug that all the
unit tests had missed.

## Official documentation

- [Rate limits for the REST API](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api) — what counts against the budget and what does not.
- [Best practices for using the REST API](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api) — conditional requests and secondary limits.
- [Using pagination in the REST API](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api) — the `Link` header.
