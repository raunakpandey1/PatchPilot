# ADR-020 — MCP exposes read-only capabilities only

**Status:** Accepted · **Phase:** 12 · **Date:** 2026-09-18

## Context

PatchPilot's capabilities are useful to other agents: repository analysis, issue
ranking, semantic code search, policy checking, injection scanning. MCP is the
standard way to offer them.

It can also generate patches, apply them, and — with approval — commit and open
pull requests.

## Options considered

1. **Expose everything.** Maximum usefulness.
2. **Expose everything, with approval still enforced internally.**
3. **Expose read-only capabilities only.**
4. **No MCP server.**

## Decision

**Read-only only**: `analyze_repository`, `find_issues`, `search_repository`,
`check_patch_policy`, `scan_for_injection`.

## Reasoning

Option 2 is the tempting one and it is wrong. The argument for it is that human
approval is enforced inside the graph, so exposing patch generation over MCP
would still stop at the approval node.

But that reasoning confuses *where the check is* with *who is being checked*. An
MCP client is an arbitrary program — often another agent — invoking tools
autonomously. Exposing a mutating capability to it means:

- the caller's context, not a person's, decides when to invoke it
- there is no reviewer looking at a diff, because there is no human in that loop
  at all
- the approval step's value came from a person seeing the change, and there is
  no person

**A tool an arbitrary client can invoke is a tool with no human in the loop**,
and the human is the most important control in this system
([ADR-018](ADR-018-human-in-the-loop.md)). Putting `create_pull_request` behind
MCP routes around it — not by breaking it, but by removing the context that gave
it meaning.

Option 4 loses something real: the read-only capabilities are genuinely useful
and carry no such risk. `search_repository` returning code is no more dangerous
than `grep`.

`check_patch_policy` is worth exposing for a slightly subtle reason: it lets
another agent find out what is permitted *before* proposing something, which is
more useful than being refused afterwards. It decides nothing and changes
nothing.

## Tradeoffs

**Against:**

- **Less useful.** Another agent cannot use PatchPilot to actually fix an issue,
  only to investigate one.
- **The line is judgement, not a rule.** "Read-only" is clear here; in a system
  with side-effecting reads it would need more thought.

**For:** no capability is reachable without a human that was supposed to require
one; the exposed surface is small enough to audit by reading it.

## Consequences

- Handlers are plain functions with a thin protocol layer over them, so they are
  testable without a protocol and the MCP layer stays a translation.
- A test asserts handlers stay small — a handler with real logic is logic that
  exists only over the protocol.
- `scan_for_injection` returns its own caveat in the response, because a clean
  result read as "safe" is exactly the misreading that makes a weak control
  dangerous.
- A test walks the source tree asserting nothing outside `mcp/` imports MCP,
  which is what turns "we kept the boundary clean" into a fact.

## Interview questions

**Q: What did you expose over MCP and what did you leave out?**

Read-only capabilities only — repository analysis, issue ranking, code search,
policy checking, injection scanning. No patch generation, commits or pull
requests. The reason is that an MCP client is an arbitrary program calling tools
autonomously, so a mutating tool exposed there has no human in the loop, and
human approval is the most important control in the system.

**Q: But approval is enforced inside your graph. Why not expose it anyway?**

Because that confuses where the check lives with who is being checked. The
approval node's value came from a person looking at a diff; over MCP there is no
person in that loop at all. The check would still execute and would be
protecting nobody.

**Q: Why expose the policy checker?**

Because it lets another agent ask what is permitted before proposing something,
rather than being refused after. It decides nothing and changes nothing — it is
strictly information, and information about the rules is the safest thing to
share.

**Q: When is MCP unnecessary complexity?**

When you have one client, which is most projects. If only your own agent calls
your tools, MCP is serialisation between two pieces of code that could call each
other directly. It earns its place when tools are reusable by clients you do not
control, and the failure mode to watch for is business logic starting to take
protocol types as parameters — at that point it has stopped being an adapter.

## Behavioural question this answers

> *"Tell me about a time you deliberately made something less capable."*

I built an MCP server so other agents could use PatchPilot's tools, and exposed
only the read-only half. The tempting argument was that mutating tools would
still hit my human-approval step, so exposing them was safe. I decided that was
wrong: an MCP client is an arbitrary program calling tools on its own, so the
approval node would still run and would be protecting nobody — the value came
from a person looking at a diff, and over a protocol there is no person. So the
server can investigate an issue and cannot fix one. It is less useful and it does
not have a path that routes around the control the whole design depends on.
