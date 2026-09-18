# ADR-016 — Docker for sandboxing, and separating install from run

**Status:** Accepted · **Phase:** 6 · **Date:** 2026-09-18

## Context

Validating a patch means running the repository's test suite. That suite is code
written by strangers, which PatchPilot has just modified. It can read the
filesystem, open sockets, allocate without limit, fork without limit, or never
terminate — none of which requires malice.

The machine is an 8 GB M1 laptop.

## Options considered

1. **Run on the host** in a subprocess with a timeout.
2. **A separate OS user** with restricted permissions.
3. **Docker container.**
4. **A virtual machine** (Lima, UTM).
5. **gVisor or Firecracker** — kernel-level isolation without a full VM.
6. **Delegate to CI** — push a branch and let GitHub Actions run it.

## Decision

**Docker**, with every control set explicitly, and **installation separated from
execution** as distinct trust levels.

## Reasoning

**Host execution is not an option.** A test can read `~/.ssh/id_rsa`. No timeout
fixes that.

**A separate user is real but partial.** It stops filesystem access to your home
directory; it does not stop network egress, and resource limits require cgroups
anyway — at which point you are most of the way to a container.

**A VM is the honest answer to a determined attacker** and costs gigabytes of
RAM and minutes of boot on a machine with 8 GB total. gVisor is the right middle
ground and is not available on macOS/ARM in a way that is practical here.

**CI delegation is genuinely good** and is what Phase 13's deployment uses,
because free CI gives real per-job containers. It is wrong for the *development*
loop: pushing a branch per attempt is slow and pollutes the repository.

So: Docker, with the limitations written down rather than glossed.

### The part that matters more than the choice

Installing dependencies **requires network access** and **executes arbitrary
code** — `setup.py` runs, and so do build hooks.

That means installation is not a safe preparatory step before the dangerous
part. It *is* a dangerous part, and treating the two as one trust level is the
mistake this design exists to avoid:

```
BUILD (network ON, once)        RUN (network OFF, every time)
pip install, setup.py runs  →   pytest, no egress, no host fs
```

Both in containers. Only one has network. The image is reused.

## The controls, and why each

| Control | Closes |
|---|---|
| `network_disabled=True` | exfiltration; fetching a second stage |
| `read_only=True` + tmpfs `/tmp` | modifying the image |
| `mem_limit` **=** `memswap_limit` | runaway allocation, and escaping it via swap |
| `pids_limit` | fork bombs |
| `nano_cpus` | monopolising the machine |
| `cap_drop=["ALL"]` | mount, raw sockets, anything privileged |
| `no-new-privileges` | escalation via setuid |
| non-root `user` | root-owned files in the mount |
| external wall-clock timeout | tests that never finish |

Each set explicitly rather than inherited, because a default that changes in a
future Docker version is a control you silently stopped having.

## Tradeoffs

**Against:**

- **A container is not a VM.** Shared kernel, so a kernel exploit escapes. Stated
  in the module docstring and in the concept doc rather than left implied.
- **Docker Desktop costs RAM** on an 8 GB machine, which is why sandbox limits
  are modest (2 GB, 2 CPUs).
- **Image build is slow** — ~16 s for a trivial repository, longer for real ones.
  Amortised by reuse.
- **The build step has network.** By necessity. A malicious `setup.py` can phone
  home during installation.

**For:** real isolation for the realistic threats, on hardware that exists, with
an escape hatch (CI delegation) already designed for deployment.

## Consequences

- `ValidationResult.passed` is False when `sandbox_error` is set: "we could not
  tell" and "it works" must never be the same value.
- The exit code decides the outcome; output parsing only enriches it.
- pytest exit code 5 (no tests collected) is an ERROR, not a pass — nothing was
  verified.
- Six integration tests assert the controls hold against real Docker, including
  one that tries to open a socket and must fail.

## Interview questions

**Q: How do you run untrusted tests safely?**

In a container with every control set explicitly: no network, read-only root plus
a tmpfs, memory equal to memory-plus-swap so the limit cannot be escaped, a PID
limit, all capabilities dropped, no-new-privileges, a non-root user, and a
wall-clock timeout enforced from outside because Docker has none of its own. The
most valuable is no network, because a test that can reach the internet can
exfiltrate whatever the process can see.

**Q: You need the network to install dependencies. How does that work?**

That is the design's actual centre. Installation needs egress *and* runs
arbitrary code, since `setup.py` executes — so it is not a safe prelude to the
dangerous part, it is a dangerous part. It happens once, in its own container, at
image build; every test run afterwards uses that image with no network at all.

**Q: Why not a VM?**

Because a container is enough for the threats I actually face — accidental damage
and opportunistic exfiltration — and a VM costs gigabytes of RAM and minutes of
boot on a machine with 8 GB. I say the limitation out loud: a container shares
the host kernel, so a kernel exploit escapes it, and against a targeted attacker
I would need gVisor or a VM. What I would not do is imply the container covers
that case.

**Q: How do you know the controls are real?**

Six integration tests against real Docker, including one that runs inside the
sandbox and tries to connect to 1.1.1.1 — it fails the suite if the connection
succeeds. Passing `network_disabled=True` is a claim about my code; that test is
a claim about Docker's behaviour, which is the part that protects anything.

## Behavioural question this answers

> *"Tell me about a security decision where you had to state the limits of your
> own solution."*

I sandbox untrusted test execution in Docker, and a container is not a virtual
machine — it shares the host kernel, so a kernel exploit escapes it. I wrote that
into the module docstring and the documentation rather than describing the
controls and letting a reader infer more safety than exists. The reasoning was
that the realistic threats for running public repository tests are accidents and
opportunistic exfiltration, both of which the container stops, and a VM would
cost gigabytes on an 8 GB machine. Stating the boundary is what makes the rest of
the claim trustworthy — a security section that lists only successes is not one I
would believe from someone else.
