"""The policy engine: what the agent is allowed to do.

The distinction this module exists to make
-------------------------------------------
There are four different things people call "guardrails", and conflating them is
how agents end up unsafe while appearing careful:

**1. Prompt instructions.** "Never delete a test." A *request*. The model
usually complies. It is also the layer an attacker gets to argue with, because
injected text sits in the same channel as your instructions.

**2. Application-level authorization.** "This function refuses to write outside
the workspace." Enforced by code that runs regardless of what any model decided.
Cannot be argued with, because there is no argument — there is an `if`.

**3. Policy enforcement.** This module. A separate, declarative layer that
inspects a *proposed action* and returns allow / require-approval / deny, with a
reason. Distinct from (2) because it is centralised and auditable: you can read
the policy without reading the code that happens to implement each action.

**4. Sandbox isolation.** "Even if it runs, it cannot reach the network."
Enforced by the kernel, outside the process entirely. The only layer that still
holds when everything above it has been defeated.

They are ordered by how much an attacker has to defeat. Prompt instructions stop
accidents. Sandboxes stop attacks. **A system with only the first has no
security, only good manners.**

PatchPilot uses all four. This file is the third.

Why a policy engine rather than checks scattered in the nodes
--------------------------------------------------------------
Because the question "what can this agent do to my repository?" should have one
answer, in one file, readable in a minute. Scattered checks are individually
correct and collectively unknowable — and the failure log for this project
already contains four bugs that were exactly that shape.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from enum import StrEnum
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict

from patchpilot.logging import get_logger
from patchpilot.models import CodeEdit, Patch

log = get_logger("guardrails")


class Decision(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


class Action(StrEnum):
    """Every consequential thing the agent can attempt.

    Enumerated rather than free-form so the policy is exhaustive by
    construction: a new capability must be added here, which makes adding one a
    visible change rather than an accident.
    """

    READ_FILE = "read_file"
    MODIFY_SOURCE = "modify_source"
    MODIFY_TEST = "modify_test"
    MODIFY_CI = "modify_ci"
    DELETE_FILE = "delete_file"
    RUN_TESTS = "run_tests"
    CREATE_BRANCH = "create_branch"
    COMMIT = "commit"
    PUSH = "push"
    PUSH_TO_DEFAULT_BRANCH = "push_to_default_branch"
    FORCE_PUSH = "force_push"
    CREATE_PULL_REQUEST = "create_pull_request"


class Verdict(BaseModel):
    """A decision with its reason.

    The reason is not decoration: it goes on the approval screen, into the logs,
    and into the safety metric. A denial nobody can explain is a denial nobody
    will trust, and it will eventually be switched off.
    """

    model_config = ConfigDict(frozen=True)

    action: Action
    decision: Decision
    reason: str
    target: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def blocked(self) -> bool:
        return self.decision is Decision.DENY

    def __str__(self) -> str:
        target = f" [{self.target}]" if self.target else ""
        return f"{self.decision.upper()}: {self.action}{target} — {self.reason}"


# Paths that change what runs, rather than what the code does. Modifying CI is
# how a patch escalates from "changes this project" to "changes what happens on
# every future commit, on someone else's infrastructure".
PROTECTED_PATHS = (
    ".github/**",
    ".gitlab-ci.yml",
    "*.yml",
    "*.yaml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    ".circleci/**",
    "Dockerfile*",
    "docker-compose*",
    "Makefile",
    "setup.py",
    "conftest.py",
    "**/conftest.py",
    ".pre-commit-config.yaml",
)

# Paths that must never be touched by a generated patch under any circumstances.
FORBIDDEN_PATHS = (
    ".git/**",
    "**/.ssh/**",
    "**/.aws/**",
    "*.pem",
    "*.key",
    "id_rsa*",
    ".env",
    "**/.env",
    ".npmrc",
    ".pypirc",
    "**/credentials*",
)

# Edits whose *content* weakens verification. A patch that deletes the assertion
# proving a bug is fixed has not fixed the bug.
TEST_WEAKENING = (
    re.compile(r"@pytest\.mark\.(skip|xfail)", re.IGNORECASE),
    re.compile(r"\bunittest\.skip\b", re.IGNORECASE),
    re.compile(r"\bpytest\.skip\s*\(", re.IGNORECASE),
    re.compile(r"^\s*#\s*assert\b", re.MULTILINE),
)

# Strings that should never appear in generated code. Not exhaustive — this is a
# tripwire, not a sandbox — but each has no legitimate reason to be introduced
# by a bug fix.
DANGEROUS_CODE = (
    re.compile(r"\bos\.system\s*\(", re.IGNORECASE),
    re.compile(r"\bsubprocess\.(run|call|Popen)\s*\(.*shell\s*=\s*True", re.IGNORECASE),
    re.compile(r"\beval\s*\(", re.IGNORECASE),
    re.compile(r"\b__import__\s*\(", re.IGNORECASE),
    re.compile(r"\brequests\.(get|post)\s*\(", re.IGNORECASE),
    re.compile(r"\burllib\.request\.urlopen\s*\(", re.IGNORECASE),
    re.compile(r"\bsocket\.(socket|create_connection)\s*\(", re.IGNORECASE),
)

# Anything shaped like a credential appearing in a diff.
#
# **This is a denylist, and denylists lag.** Google issued a new key format
# (`AQ.` followed by base64-ish characters) that the original patterns here did
# not match — discovered by testing this detector against a freshly rotated key
# rather than by it firing. A provider can introduce a format tomorrow and this
# list will not know about it.
#
# So the last pattern is deliberately *shape-based* rather than
# vendor-specific: a long opaque value assigned to something named like a
# credential. It catches formats nobody has enumerated, at the cost of some
# false positives — which is the right direction for this trade, because the
# consequence of a miss is a published secret and the consequence of a false
# positive is a human looking at a diff.
SECRET_PATTERNS = (
    # Vendor-specific prefixes: precise, and only as current as this list.
    re.compile(r"\b(?:sk|pk|ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{16,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),          # Google, legacy format
    re.compile(r"\bAQ\.[A-Za-z0-9_\-]{30,}"),           # Google, current format
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),                 # AWS access key id
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),      # Slack
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    # Shape-based: a long opaque value assigned to a credential-shaped name.
    # Format-agnostic on purpose, because the patterns above will always lag.
    # `\w*` on both sides so `service_token` and `my_api_key` match too — an
    # underscore is a word character, so a leading \b would only match names
    # that *start* with the keyword.
    re.compile(
        r"(?i)\b\w*(?:api[_-]?key|secret|password|passwd|token|credential|auth)\w*"
        r"\s*[:=]\s*['\"][A-Za-z0-9_\-./+]{16,}['\"]"
    ),
)


class Policy:
    """Decides whether a proposed action is permitted.

    Stateless and pure: same inputs, same verdict. That matters because the
    safety metric in Phase 11 counts blocked actions, and a policy that varied
    would make that number meaningless.
    """

    def __init__(
        self,
        *,
        protected_paths: Sequence[str] = PROTECTED_PATHS,
        forbidden_paths: Sequence[str] = FORBIDDEN_PATHS,
        allow_test_modification: bool = True,
    ) -> None:
        self._protected = tuple(protected_paths)
        self._forbidden = tuple(forbidden_paths)
        # Tests may legitimately need changing — a bug fix often adds one. What
        # is never allowed is *weakening* an existing one, checked by content.
        self._allow_test_modification = allow_test_modification

    # --- single actions ----------------------------------------------------

    def check_action(self, action: Action, target: str = "") -> Verdict:
        """The decision table, stated once.

        Read top to bottom, this is the complete answer to "what can this agent
        do to my repository?"
        """
        if action in (Action.READ_FILE, Action.RUN_TESTS):
            return self._verdict(action, Decision.ALLOW, "read-only operation", target)

        if action is Action.FORCE_PUSH:
            # Never, under any approval. A force push can destroy history that
            # exists nowhere else, and no fix requires one.
            return self._verdict(
                action, Decision.DENY,
                "force push can destroy history irrecoverably and is never required by a fix",
                target,
            )

        if action is Action.PUSH_TO_DEFAULT_BRANCH:
            return self._verdict(
                action, Decision.DENY,
                "pushing directly to the default branch bypasses review entirely",
                target,
            )

        if action is Action.MODIFY_CI:
            return self._verdict(
                action, Decision.REQUIRE_APPROVAL,
                "CI configuration controls what runs on every future commit",
                target,
            )

        if action is Action.DELETE_FILE:
            return self._verdict(
                action, Decision.REQUIRE_APPROVAL,
                "deleting a file is rarely part of a bug fix and is hard to review",
                target,
            )

        if action in (
            Action.CREATE_BRANCH, Action.COMMIT, Action.PUSH, Action.CREATE_PULL_REQUEST
        ):
            return self._verdict(
                action, Decision.REQUIRE_APPROVAL,
                "changes another person's repository and requires a human decision",
                target,
            )

        if action is Action.MODIFY_TEST and not self._allow_test_modification:
            return self._verdict(
                action, Decision.REQUIRE_APPROVAL, "test modification is disabled", target
            )

        # MODIFY_SOURCE and MODIFY_TEST: permitted in the sandbox. Nothing here
        # reaches the real repository — that requires COMMIT and PUSH above.
        return self._verdict(
            action, Decision.ALLOW, "sandboxed change, not applied to the repository", target
        )

    def check_path(self, file_path: str) -> Verdict:
        """Decide whether a path may be written at all."""
        normalised = self._normalise(file_path)

        if normalised is None:
            return self._verdict(
                Action.MODIFY_SOURCE, Decision.DENY,
                "path escapes the repository root", file_path,
            )

        if self._matches(normalised, self._forbidden):
            return self._verdict(
                Action.MODIFY_SOURCE, Decision.DENY,
                "path holds credentials or version-control internals", normalised,
            )

        if self._matches(normalised, self._protected):
            return self._verdict(
                Action.MODIFY_CI, Decision.REQUIRE_APPROVAL,
                "path controls build, CI or test collection behaviour", normalised,
            )

        return self._verdict(Action.MODIFY_SOURCE, Decision.ALLOW, "ordinary source path", normalised)

    # --- whole patches -----------------------------------------------------

    def check_patch(self, patch: Patch, *, test_files: Sequence[str] = ()) -> list[Verdict]:
        """Every objection to a patch, as a list.

        Returns all findings rather than the first, because an approval screen
        showing one problem at a time is a screen that gets clicked through.
        """
        verdicts: list[Verdict] = []
        tests = set(test_files)

        for edit in patch.edits:
            verdicts.append(self.check_path(edit.file_path))

            if edit.file_path in tests or _looks_like_test(edit.file_path):
                verdicts.extend(self._check_test_edit(edit))

            verdicts.extend(self._check_content(edit))

        verdicts.extend(self._check_secrets(patch))
        return [v for v in verdicts if not v.allowed]

    def evaluate_patch(self, patch: Patch, *, test_files: Sequence[str] = ()) -> Decision:
        """The single worst verdict across a patch."""
        objections = self.check_patch(patch, test_files=test_files)
        if any(v.blocked for v in objections):
            return Decision.DENY
        if objections:
            return Decision.REQUIRE_APPROVAL
        return Decision.ALLOW

    # --- content checks ----------------------------------------------------

    def _check_test_edit(self, edit: CodeEdit) -> list[Verdict]:
        """Catch a patch that makes a test pass by making it stop testing.

        The check is on *direction*: introducing a skip marker is a denial;
        removing one is fine. And a patch that deletes assertions without adding
        any is removing verification, which is the subtle version of the same
        move.
        """
        verdicts: list[Verdict] = []

        for pattern in TEST_WEAKENING:
            introduced = len(pattern.findall(edit.new_text)) - len(pattern.findall(edit.old_text))
            if introduced > 0:
                verdicts.append(self._verdict(
                    Action.MODIFY_TEST, Decision.DENY,
                    f"introduces {pattern.pattern} — makes a test stop verifying",
                    edit.file_path,
                ))

        removed = edit.old_text.count("assert ") - edit.new_text.count("assert ")
        if removed > 0 and not edit.new_text.strip():
            verdicts.append(self._verdict(
                Action.MODIFY_TEST, Decision.DENY,
                f"deletes {removed} assertion(s) without replacement",
                edit.file_path,
            ))
        elif removed > 0:
            verdicts.append(self._verdict(
                Action.MODIFY_TEST, Decision.REQUIRE_APPROVAL,
                f"removes {removed} assertion(s) — verify this is not weakening the test",
                edit.file_path,
            ))

        return verdicts

    def _check_content(self, edit: CodeEdit) -> list[Verdict]:
        """Flag newly introduced dangerous constructs.

        On the *difference*, not the absolute content: a file that already calls
        `subprocess` is not made worse by an unrelated edit, and flagging it
        would make the check noise that gets ignored.
        """
        return [
            self._verdict(
                Action.MODIFY_SOURCE, Decision.REQUIRE_APPROVAL,
                f"introduces {pattern.pattern} — network or code execution in a bug fix",
                edit.file_path,
            )
            for pattern in DANGEROUS_CODE
            if len(pattern.findall(edit.new_text)) > len(pattern.findall(edit.old_text))
        ]

    def _check_secrets(self, patch: Patch) -> list[Verdict]:
        """Refuse a patch that would commit a credential."""
        added = "\n".join(
            line for line in patch.diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        return [
            self._verdict(
                Action.COMMIT, Decision.DENY,
                "the patch adds something shaped like a credential", "",
            )
            for pattern in SECRET_PATTERNS
            if pattern.search(added)
        ][:1]

    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _normalise(file_path: str) -> str | None:
        """Reject traversal, then return a comparable path.

        Done here as well as in the workspace because defence in depth means the
        check exists in more than one place — the workspace protects the
        filesystem, this protects the *policy decision*.
        """
        if file_path.startswith("/") or ".." in PurePosixPath(file_path).parts:
            return None
        return str(PurePosixPath(file_path))

    @staticmethod
    def _matches(path: str, patterns: Sequence[str]) -> bool:
        name = PurePosixPath(path).name
        return any(
            fnmatch.fnmatch(path, pattern)
            or fnmatch.fnmatch(name, pattern)
            or (pattern.endswith("/**") and path.startswith(pattern[:-3] + "/"))
            for pattern in patterns
        )

    @staticmethod
    def _verdict(action: Action, decision: Decision, reason: str, target: str) -> Verdict:
        verdict = Verdict(action=action, decision=decision, reason=reason, target=target)
        if decision is not Decision.ALLOW:
            log.info(
                "policy_decision",
                action=str(action), decision=str(decision), target=target, reason=reason,
            )
        return verdict


def _looks_like_test(file_path: str) -> bool:
    from patchpilot.analysis.repository import is_test_path

    return is_test_path(file_path)
