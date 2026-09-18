# MCP — what it solves, and when it is overhead

## In one sentence

The Model Context Protocol is a standard way for a program to describe its tools
to an AI application, turning N×M custom integrations into N+M.

## The problem it solves

You have built something useful — say, semantic search over a repository. You
want models to be able to call it.

Before MCP, each AI application had its own tool format. Supporting three
applications meant writing three adapters. Ten tool providers times ten
applications is a hundred integrations, each maintained by somebody.

MCP is a wire protocol for that description. A **server** advertises tools with
JSON Schema; a **client** discovers and calls them. Write the server once, any
client can use it.

That is the whole value proposition, and it is a real one. It is the same
argument as the Language Server Protocol for editors, which is explicitly where
the design comes from — and LSP genuinely did collapse an N×M problem.

## How it works, step by step

Three concepts, and only the first matters most of the time:

**Tools** — functions a model can call. Name, description, JSON Schema for
arguments.

**Resources** — data a client can read (files, records). Closer to a GET than a
function call.

**Prompts** — reusable prompt templates the server offers.

A session is: client connects → asks what tools exist → model chooses one →
client calls it → result goes back into the conversation. Transport is usually
stdio (a subprocess) or HTTP.

### The description is the interface

This is the part people under-invest in. A model picks a tool by **reading its
description**, so the description is not documentation — it is the API.

```python
# Bad — restates the signature
"description": "Searches a repository."

# Better — says when to use it and what comes back
"description": (
    "Search an indexed repository's code by meaning rather than keywords. "
    "Returns file paths, line ranges and the code itself. The repository "
    "must have been indexed first."
)
```

The second tells a model when *not* to reach for it, which is most of the value.

## What MCP does not solve

It standardises **how a tool is described and called**. It says nothing about
whether calling it is a good idea.

Authorization, rate limiting, auditing and blast radius are all still yours. A
server that exposes `delete_repository` over MCP has exposed
`delete_repository` — the protocol adds no safety, and the fact that a call
arrived over a standard protocol says nothing about who sent it or why.

This matters because MCP makes tools *easy to connect*, and ease of connection
is exactly what turns a capability into an attack surface.

## The decision made here

PatchPilot's MCP server exposes **only read-only capabilities**:

```
analyze_repository     find_issues       search_repository
check_patch_policy     scan_for_injection
```

Absent: patch generation, commits, pull requests.

Not because MCP could not carry them. Because **a tool an arbitrary client can
invoke is a tool with no human in the loop**, and the human is the control
([guardrails](guardrails-vs-prompts.md)). Adding `create_pull_request` to an MCP
server would route around the single most important safeguard in the system.

## When MCP is unnecessary complexity

Worth being able to say plainly, because "we added MCP" is currently a thing
people say to sound modern:

**When you have one client.** Which is most of the time. If your tools are
called only by your own agent, MCP is a serialisation layer between two pieces
of code that could call each other directly. That is cost with no benefit.

**When the tools are not reusable.** MCP pays off when someone you do not
control benefits from your tools. `search_repository` plausibly qualifies.
`apply_edit_to_working_copy_number_three` does not.

**When it becomes the architecture.** The failure mode is business logic that
starts assuming it is being called over MCP — taking MCP types as parameters,
returning MCP content blocks. Then the protocol is no longer an adapter and
removing it is a rewrite.

## The test that proves it stayed an adapter

PatchPilot's architectural rule from Phase 0 was that business logic never
imports its transport. The MCP layer is where that claim gets tested, so there
is a test that walks the source tree:

```python
def test_no_business_logic_imports_mcp():
    for path in source_root.rglob("*.py"):
        if "mcp" in path.parts:
            continue
        assert "import mcp" not in path.read_text()
```

Deleting `src/patchpilot/mcp/` would change no behaviour anywhere else. That is
what makes MCP an adapter here rather than an architecture — and it is the
honest answer to whether the boundary held, rather than an assertion that it did.

## In PatchPilot

- [`mcp/server.py`](../../src/patchpilot/mcp/server.py) — handlers as plain
  functions, with a thin protocol layer over them.
- [`tests/unit/test_mcp.py`](../../tests/unit/test_mcp.py) — including the
  boundary test above.

Handlers are ordinary functions rather than decorated ones, so they are testable
without a protocol and so the MCP layer stays a translation. There is a test
asserting they stay small — a handler with real logic in it is logic that only
exists over the protocol.

## Interview questions

**Q: What problem does MCP solve?**

An N×M integration problem. Before it, every AI application had its own tool
description format, so every tool provider wrote an adapter per application. MCP
standardises the description and the call, making it N+M. It is the same
argument as LSP for editors, and it came from the same observation.

**Q: What does MCP not give you?**

Safety. It standardises how a tool is described and invoked and says nothing
about whether invoking it is wise. Authorization, rate limiting and blast radius
remain entirely yours — a server exposing a destructive tool has exposed it, and
the call arriving over a standard protocol tells you nothing about who sent it.

**Q: What did you expose, and what did you deliberately not?**

Only read-only capabilities: repository analysis, issue ranking, code search,
policy checking, injection scanning. No patch generation, commits or pull
requests — because a tool an arbitrary client can call has no human in the loop,
and human approval is the most important control in the system. Putting
`create_pull_request` behind MCP would route around it.

**Q: When is MCP unnecessary complexity?**

When you have one client, which is most projects. If only your own agent calls
your tools, MCP is serialisation between two pieces of code that could call each
other directly. It earns its place when tools are genuinely reusable by clients
you do not control. The failure mode to watch for is business logic starting to
take MCP types as parameters — at that point it has stopped being an adapter.

**Q: How do you know it stayed an adapter?**

A test that walks the source tree and asserts nothing outside the MCP directory
imports MCP. Deleting that directory changes no behaviour. It is a cheap test
and it is the only thing that turns "we kept the boundary clean" from an
intention into a fact.

## Official documentation

- [Model Context Protocol — Introduction](https://modelcontextprotocol.io/introduction) — the protocol's own framing of the N×M problem.
- [MCP — Server concepts](https://modelcontextprotocol.io/docs/concepts/architecture) — tools, resources and prompts.
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — the implementation used here.
