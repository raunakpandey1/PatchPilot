# Running someone else's code without it running you

## In one sentence

To find out whether a patch works you must run the repository's test suite, and
that test suite is code written by strangers — so it runs in a container that
cannot reach the network, cannot see your filesystem, and gets killed if it
does not finish.

## The problem it solves

PatchPilot's whole purpose requires executing untrusted code. There is no way
around it: "does this patch fix the bug?" is only answerable by running the
tests.

A test suite is arbitrary Python. It can:

- read `~/.ssh/id_rsa` and POST it somewhere
- delete files
- allocate memory until the machine swaps
- fork until the process table is full
- simply never terminate

None of this requires malice. A test that accidentally loops forever does the
same damage to your afternoon as one that does it deliberately.

And "the repository looks reputable" is not a control. Neither is "I read the
test file" — dependencies run code too.

## How it works, step by step

### The controls, and what each one closes

| Control | Closes |
|---|---|
| `network_disabled=True` | exfiltration, and fetching a second-stage payload |
| `read_only=True` + tmpfs `/tmp` | modifying the image; writes go to memory that vanishes |
| `mem_limit` = `memswap_limit` | runaway allocation, *and* escaping the limit via swap |
| `pids_limit` | fork bombs |
| `nano_cpus` | one test monopolising the machine |
| `cap_drop=["ALL"]` | mounting, raw sockets, anything privileged |
| `no-new-privileges` | escalation through a setuid binary |
| non-root `user` | files created in the mount belonging to root |
| external wall-clock timeout | tests that never finish |

Each is set explicitly rather than relying on Docker's defaults, because a
default that changes in a future version is a control you did not have.

### The split that matters most: install versus run

Here is the part people get wrong.

Installing dependencies **requires the network**. It also **executes arbitrary
code** — `setup.py` runs, and so do build hooks.

So installing is not a safe preparatory step that happens before the dangerous
part. It *is* a dangerous part.

PatchPilot therefore separates them into two different trust levels:

```
  BUILD (network ON)          RUN (network OFF)
  ┌────────────────┐          ┌────────────────┐
  │ pip install    │  image   │ pytest         │
  │ setup.py runs  ├─────────►│ no egress      │
  │ in a container │          │ no host fs     │
  └────────────────┘          └────────────────┘
```

Both happen inside containers. Only one has network, once, and the image it
produces is reused for every test run.

### Why the timeout is enforced from outside

Docker has no built-in run timeout. A `sleep(600)` in a test would hold the
container — and the run — open indefinitely. So the client waits with a
deadline and kills the container when it expires.

## Verified, not assumed

This is the part that separates a security control from a security *claim*.
Passing `network_disabled=True` is a statement about our code. Whether Docker
honours it is a different question, so there is a live test that answers it:

```python
def test_cannot_reach_the_internet():
    socket.setdefaulttimeout(5)
    try:
        socket.create_connection(('1.1.1.1', 53), timeout=5)
    except OSError:
        return                      # expected
    raise AssertionError('NETWORK WAS REACHABLE — the sandbox is not isolating')
```

That test runs *inside* the sandbox, against real Docker, and fails loudly if
isolation ever breaks. Same for the filesystem, the user id, and the timeout —
see [`tests/integration/test_sandbox_live.py`](../../tests/integration/test_sandbox_live.py).

## What this does not protect against

Worth stating plainly, because a security section that lists only successes is
not trustworthy.

**A container shares the host kernel.** A kernel exploit escapes it. Docker
isolation is namespaces and cgroups, not virtualisation.

For running tests from public repositories, that is an accepted risk: the
realistic threats are accidental damage and opportunistic exfiltration, and the
controls above stop both. A party specifically targeting this system with a
kernel 0-day is out of scope, and the honest mitigation would be a VM, gVisor,
or Firecracker.

**The image build has network.** By necessity. A malicious `setup.py` can phone
home during installation. Mitigated only by it being a container, and by that
step happening once rather than per test run.

## In PatchPilot

- [`sandbox/docker_runner.py`](../../src/patchpilot/sandbox/docker_runner.py) —
  the controls and the two-phase build.
- [`sandbox/parsing.py`](../../src/patchpilot/sandbox/parsing.py) — turning
  output into structured failures.
- [`tests/integration/test_sandbox_live.py`](../../tests/integration/test_sandbox_live.py)
  — six tests against real Docker.

## What goes wrong

**Treating a sandbox error as a pass.** "We could not tell" and "it works" must
never be the same value, or a broken daemon silently approves every patch.
`ValidationResult.passed` returns False when `sandbox_error` is set.

**Trusting the parser over the exit code.** Output formats vary; exit codes do
not. The exit code decides the outcome and parsing only enriches it.

**Treating "no tests collected" as success.** pytest exit code 5 means nothing
was verified. A patch that breaks test collection would otherwise look
validated.

**Bind-mounting more than you need.** Only the disposable working copy is
mounted. Mounting the repository root would expose `.git`; mounting a parent
directory would expose everything.

## Interview questions

**Q: You have to run untrusted tests. How?**

In a container with every control set explicitly: no network, read-only root
with a tmpfs `/tmp`, memory equal to memory-plus-swap so the limit cannot be
escaped, a PID limit against fork bombs, all capabilities dropped,
no-new-privileges, a non-root user, and a wall-clock timeout enforced from
outside because Docker has none. The most valuable of those is no network — a
test that can reach the internet can exfiltrate anything the process can see.

**Q: Installing dependencies needs the network. How do you handle that?**

By treating install and run as different trust levels. Installation needs egress
*and* executes arbitrary code — `setup.py` runs — so it happens once, in its own
container, at image build. Test runs then use that image with no network at all.
Treating installation as a safe preparatory step is the mistake the split exists
to avoid.

**Q: How do you know the isolation actually works?**

There is a test inside the sandbox that tries to open a socket to 1.1.1.1 and
fails the build if it succeeds. Same for the host filesystem, the user id, and
the timeout. Passing `network_disabled=True` is a claim about my code; the test
is a claim about Docker's behaviour, which is the part that protects anything.

**Q: What does a container not protect you from?**

A kernel exploit — containers share the host kernel, so isolation is namespaces
and cgroups rather than virtualisation. For public repository tests I accept
that: the realistic threats are accidents and opportunistic exfiltration, both
of which are stopped. A targeted attacker with a kernel 0-day would need a VM or
gVisor, and I would say so rather than imply the container was enough.

## Official documentation

- [Docker — Runtime options with memory, CPUs and GPUs](https://docs.docker.com/engine/containers/resource_constraints/) — the limit flags and what each enforces.
- [Docker — Container networking](https://docs.docker.com/engine/network/) — what `--network none` actually does.
- [Docker — Security](https://docs.docker.com/engine/security/) — capabilities, no-new-privileges, and the kernel caveat stated by Docker itself.
