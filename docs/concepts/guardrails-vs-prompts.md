# Four things called "guardrails", and why the difference matters

## In one sentence

Telling a model not to do something is a request; a function that refuses is a
control — and most "AI safety" in agent projects is the first kind wearing the
second's clothes.

## The problem it solves

You are building an agent that can modify code. You want it not to delete tests,
not to push to main, not to leak credentials.

The obvious approach is to say so in the system prompt. It mostly works. It is
also the layer an attacker gets to argue with, because injected text arrives in
the same channel as your instructions and the model has no reliable way to tell
them apart.

So the question is not "did I add guardrails?" but **"how much does an attacker
have to defeat?"**

## How it works, step by step

Four distinct mechanisms, ordered by how much they resist:

### 1. Prompt instructions

```
NEVER delete, skip or weaken a test.
```

A *request*. The model usually complies. An attacker who can get text into the
prompt can argue with it. Genuinely useful for preventing **accidents** — the
model doing something careless — and useless against **attacks**.

### 2. Application-level authorization

```python
def resolve(self, candidate: Path) -> Path:
    if not resolved.is_relative_to(self.root):
        raise WorkspaceError(...)
```

Code that refuses, regardless of what any model decided. There is no argument
because there is no conversation — there is an `if`.

### 3. Policy enforcement

A separate, declarative layer that inspects a proposed *action* and returns
allow / require-approval / deny with a reason.

Distinct from (2) because it is **centralised and auditable**. "What can this
agent do to my repository?" has one answer, in one file, readable in a minute.
Scattered checks are individually correct and collectively unknowable.

```python
if action is Action.FORCE_PUSH:
    return DENY, "can destroy history irrecoverably and is never required by a fix"
```

### 4. Sandbox isolation

```python
network_disabled=True, read_only=True, cap_drop=["ALL"]
```

Enforced by the kernel, outside the process entirely. Still holds when every
layer above it has been defeated — including when the model has been fully
compromised and is deliberately writing malicious code.

## The ordering, and what it means

```
prompt instructions   ← an attacker argues with this
application checks    ← an attacker must find a code path without one
policy engine         ← an attacker must find an action it does not cover
sandbox               ← an attacker must break out of a container
human approval        ← an attacker must convince a person
```

**A system with only the first has no security, only good manners.**

The design rule that follows: **assume the layer above failed.** The policy
engine is written as though the prompt were ignored. The sandbox is written as
though the policy had a hole. Human approval is written as though everything
before it was compromised.

## Some decisions worth seeing

The policy is more interesting where it *refuses to be flexible*:

**Force push is DENY, not require-approval.** Not a judgement call. A force push
can destroy history that exists nowhere else, and no bug fix needs one. Offering
a human the option to override a rule like that is how it gets overridden at 2am
by someone tired.

**Adding `@pytest.mark.skip` is DENY.** The cheapest way to make a failing test
pass is to stop it running. That is not a fix, it is a lie about one.

**Removing a skip marker is ALLOW.** The check is on *direction*. Re-enabling a
test is the opposite of weakening one, and flagging it would be exactly
backwards — this is the kind of detail that decides whether a control is useful
or just noisy.

**A credential in added lines is DENY; in removed lines it is ALLOW.** Removing
a hard-coded secret is a good patch. A check that punished it would be training
people to switch the check off.

**Dangerous constructs are checked as a diff, not an absolute.** A file that
already calls `subprocess` is not made worse by an unrelated edit. Flagging
pre-existing code makes the check noise, and noisy checks get ignored.

## In PatchPilot

- [`guardrails/policy.py`](../../src/patchpilot/guardrails/policy.py) — the
  decision table.
- [`tools/workspace.py`](../../src/patchpilot/tools/workspace.py) —
  application-level authorization.
- [`sandbox/docker_runner.py`](../../src/patchpilot/sandbox/docker_runner.py) —
  isolation.
- [`agent/nodes/approval.py`](../../src/patchpilot/agent/nodes/approval.py) —
  the human.
- [`tests/unit/test_guardrails.py`](../../tests/unit/test_guardrails.py) — 64
  tests written as a red team, including attacks that evade detection and are
  stopped anyway.

## What goes wrong

**Believing the prompt.** The most common failure. A system prompt full of
NEVERs reads as thorough and defends against nothing determined.

**A policy nobody can explain.** Every verdict here carries a reason, because a
denial without one is a denial that gets disabled the first time it is
inconvenient.

**Checks so noisy they get switched off.** A control that fires on ordinary work
has negative value: it trains people to ignore it. Hence diff-based checks and
direction-sensitive rules.

**Offering an override on a hard rule.** If something is genuinely never
acceptable, it should not appear on the approval screen at all. A denied patch
in PatchPilot halts before a human sees it.

## Interview questions

**Q: How do you stop an agent doing something dangerous?**

Four layers, and I would be specific about which is which. Prompt instructions
prevent accidents and nothing else, because an attacker's text shares a channel
with mine. Application checks are code that refuses regardless of what the model
decided. A policy engine centralises that into one auditable table, so "what can
this agent do to my repository" has one answer rather than being spread across
the codebase. And a sandbox enforces limits in the kernel, which still holds if
everything above it failed. The design rule is that each layer assumes the one
above it did not work.

**Q: What is the difference between a prompt instruction and a guardrail?**

One is a request and the other is an `if`. "Never delete a test" in a system
prompt is something the model complies with when nothing is pushing it
otherwise. A function that inspects the patch and refuses when it introduces a
skip marker is a control. The distinction matters because injected text argues
with the first and cannot argue with the second.

**Q: Give me a policy decision you deliberately made inflexible.**

Force push is denied outright rather than requiring approval. A force push can
destroy history that exists nowhere else and no bug fix needs one, so making it
a judgement call only creates an opportunity to make the wrong judgement. More
generally, if something is never acceptable it should not appear on the approval
screen — a denied patch halts before a human sees it.

**Q: How do you keep a policy from becoming noise?**

By checking direction and difference rather than absolutes. Adding a skip marker
is denied; removing one is fine. A credential in added lines is denied; the same
string in removed lines is a *good* patch. Dangerous constructs are compared
against the old text, so code a file already had is not flagged. A control that
fires on ordinary work has negative value, because it teaches people to ignore
it.

## Official documentation

- [OWASP — Top 10 for LLM Applications](https://owasp.org/www-project-top-10-for-large-language-model-applications/) — the standard threat taxonomy for exactly this.
- [NVIDIA NeMo Guardrails](https://docs.nvidia.com/nemo/guardrails/) — a framework for the policy layer, and useful for seeing what a declarative version looks like.
