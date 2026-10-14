#!/usr/bin/env python3
"""Pin the caller/callee secret contract for review-gate.yml.

The local .github/workflows/review-gate.yml invokes
.gh/workflows/review-gate-reusable.yml@<caller-pin> as a `workflow_call`.
Two surfaces MUST match, or the run fails at creation with
`startup_failure` -- and that failure reads on every PR as "this PR has
unresolved review threads", the worst possible misreport for this
particular check:

  1. Every secret the reusable declares in
     `on.workflow_call.secrets.<name>` MUST be passed by the caller
     under `jobs.<job>.secrets.<name>`. A reusable that the caller
     fails to pass errors at create-time and the routing shim never
     runs.
  2. Every secret the caller passes under `jobs.<job>.secrets.<name>`
     MUST be declared in the reusable's
     `on.workflow_call.secrets`. A name the caller passes but the
     reusable does not declare is "unexpected value" -- the same
     startup_failure re-shaped.

A future edit that drops a `secrets:` line from the caller
(verifier t_e4066aa0 REJECTed at ac14c86 before the fix), or adds
one to the reusable without adding it to the caller, must fail this
test. tests/test_review_gate_route_shim.py covers the shim's wiring
inside the reusable, and tests/test_ci_design_permissions.py covers
the caller's permissions; NEITHER covers the caller/callee secret
contract that decides whether the run starts at all.

`secrets: inherit` is also wrong here (zizmor flags it as
`secrets-inherit`; the caller lists each name explicitly), and a
regression to `secrets: inherit` MUST fail this test too: the
contract is "caller lists each secret by name".

The test follows the chain through the local caller's pin (rather
than reading the head reusable), so the test verifies the END-TO-END
runtime contract, not just the head source. A caller that pointed at
an older reusable commit whose secrets block was empty (or
incomplete) would still fail here even if the local reusable at the
head is correct.

Run directly: `python3 tests/test_review_gate_secret_contract.py`.
Run in CI: `self-check.yml`'s "Run the test suites" step.
"""

from __future__ import annotations

import pathlib
import re
import subprocess  # nosec B404
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[1]
CALLER = ROOT / ".github/workflows/review-gate.yml"
REUSABLE_PATH = ".github/workflows/review-gate-reusable.yml"

CALLER_PIN_RE = re.compile(
    r"maxi-tools/ci/" + re.escape(REUSABLE_PATH) + r"@([0-9a-f]{40})"
)


def _git_cat(commit: str, relpath: str) -> str:
    """Read `<relpath>` from the tree at `<commit>` in the local checkout."""
    proc = subprocess.run(  # nosec B603 B607  # noqa: S603 S607
        ["git", "cat-file", "-p", f"{commit}:{relpath}"],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git cat-file -p {commit}:{relpath} exited {proc.returncode}: "
            f"{proc.stderr.strip() or '<no stderr>'}"
        )
    return proc.stdout


def _caller_pin(caller_text: str) -> str:
    m = CALLER_PIN_RE.search(caller_text)
    if m is None:
        raise AssertionError(
            f"{CALLER.name}: no `uses: ...{REUSABLE_PATH}@<40-char-sha>` "
            f"line found. The local reviewer pin must be a 40-char sha; a "
            f"branch or tag reference means this test cannot follow the "
            f"chain and the caller may be silently executing an older "
            f"runtime than the head source."
        )
    pin = m.group(1)
    if len(pin) != 40:
        raise AssertionError(
            f"{CALLER.name}: extracted pin {pin!r} is not 40 chars; the "
            f"regex matched but the chain link is malformed."
        )
    return pin


class CallerCalleeSecretContractTest(unittest.TestCase):
    def setUp(self):
        caller_doc = yaml.safe_load(CALLER.read_text(encoding="utf-8"))
        if not isinstance(caller_doc, dict) or "jobs" not in caller_doc:
            raise AssertionError(
                f"{CALLER.name}: expected a top-level mapping with `jobs:`"
            )
        jobs = caller_doc["jobs"]
        if not isinstance(jobs, dict) or not jobs:
            raise AssertionError(
                f"{CALLER.name}: expected `jobs:` to be a non-empty mapping"
            )
        # The file has one job (`review-gate`) that `uses:` the reusable.
        # Find it rather than assume name, so a future rename doesn't silently
        # bypass the test.
        uses_jobs = [
            (name, job)
            for name, job in jobs.items()
            if isinstance(job, dict)
            and isinstance(job.get("uses"), str)
            and REUSABLE_PATH in job["uses"]
        ]
        if len(uses_jobs) != 1:
            raise AssertionError(
                f"{CALLER.name}: expected exactly one `uses:` job pointing "
                f"at `{REUSABLE_PATH}`; found {len(uses_jobs)}. The "
                "caller/callee secret test requires one unambiguous callee."
            )
        self.caller_name, self.caller_job = uses_jobs[0]
        self.caller_pin = _caller_pin(CALLER.read_text(encoding="utf-8"))

        reusable_doc = yaml.safe_load(_git_cat(self.caller_pin, REUSABLE_PATH))
        if not isinstance(reusable_doc, dict):
            raise AssertionError(
                f"{REUSABLE_PATH} at {self.caller_pin}: not a YAML mapping"
            )
        on = reusable_doc.get("on") or reusable_doc.get(True)  # `on:` may parse as True
        if not isinstance(on, dict):
            raise AssertionError(
                f"{REUSABLE_PATH} at {self.caller_pin}: `on:` is missing or "
                f"not a mapping (got {type(on).__name__})"
            )
        workflow_call = on.get("workflow_call")
        if not isinstance(workflow_call, dict):
            raise AssertionError(
                f"{REUSABLE_PATH} at {self.caller_pin}: `on.workflow_call:` "
                f"is missing or not a mapping"
            )
        self.reusable_secrets = workflow_call.get("secrets") or {}

    def test_caller_does_not_use_secrets_inherit(self):
        # `secrets: inherit` is what the file used to do, and zizmor
        # flags it as `secrets-inherit` (see the comment block in
        # review-gate.yml above the `secrets:` map). A regression to
        # `secrets: inherit` removes the per-name listing this test
        # pins, and the named-only contract is the explicit
        # caller/callee surface -- `secrets: inherit` does not name
        # anything, so the contract is silent.
        secrets = self.caller_job.get("secrets")
        self.assertNotIn(
            secrets,
            (None, "inherit"),
            f"{CALLER.name} `{self.caller_name}:` `secrets:` is "
            f"{secrets!r}. The caller must list each secret by name "
            f"(`APP_CLIENT_ID`, `APP_PRIVATE_KEY`) rather than use "
            f"`secrets: inherit`, which zizmor flags as "
            f"`secrets-inherit` and which leaves the caller/callee "
            f"surface unspecified.",
        )
        if not isinstance(secrets, dict):
            self.fail(
                f"{CALLER.name} `{self.caller_name}:` `secrets:` is "
                f"{secrets!r}; expected a mapping of secret name to "
                f"expression."
            )

    def test_every_reusable_declared_secret_is_passed_by_caller(self):
        # Forward direction. A reusable's `on.workflow_call.secrets`
        # listing is a declaration of what the callee ACCEPTS; the caller
        # MUST pass every declared name (named or `secrets: inherit`).
        # A missing name fails the run at creation.
        caller_secrets = self.caller_job.get("secrets") or {}
        if not isinstance(caller_secrets, dict):
            self.fail(
                f"{CALLER.name} `{self.caller_name}:` `secrets:` is "
                f"{caller_secrets!r}; expected a mapping."
            )
        reusable_names = set(self.reusable_secrets.keys())
        caller_names = set(caller_secrets.keys())
        missing = sorted(reusable_names - caller_names)
        self.assertEqual(
            missing,
            [],
            f"{CALLER.name} `{self.caller_name}:` does not pass every "
            f"secret the reusable at {self.caller_pin} declares in "
            f"`on.workflow_call.secrets:`. Missing: {missing}. Declared "
            f"by reusable: {sorted(reusable_names)}. Passed by caller: "
            f"{sorted(caller_names)}. A reusable whose declared secret "
            f"the caller does not pass fails the run at creation with "
            f"`startup_failure`, which reads on the PR as 'this PR has "
            f"unresolved review threads' -- the worst possible "
            f"misreport for this check.",
        )

    def test_every_caller_passed_secret_is_declared_by_reusable(self):
        # Reverse direction. The caller MUST NOT pass a secret the
        # reusable does not declare; the rejection is `unexpected
        # value`, the same `startup_failure` shape under a different
        # name. This is the typo / non-internet shape -- the caller
        # renames a secret in the wrong file.
        caller_secrets = self.caller_job.get("secrets") or {}
        if not isinstance(caller_secrets, dict):
            self.fail(
                f"{CALLER.name} `{self.caller_name}:` `secrets:` is "
                f"{caller_secrets!r}; expected a mapping."
            )
        reusable_names = set(self.reusable_secrets.keys())
        caller_names = set(caller_secrets.keys())
        extra = sorted(caller_names - reusable_names)
        self.assertEqual(
            extra,
            [],
            f"{CALLER.name} `{self.caller_name}:` passes secret(s) "
            f"{extra} that the reusable at {self.caller_pin} does not "
            f"declare in `on.workflow_call.secrets:`. Declared by "
            f"reusable: {sorted(reusable_names)}. Passed by caller: "
            f"{sorted(caller_names)}. A caller secret the reusable "
            f"does not declare is rejected as `unexpected value` and "
            f"the run fails at creation -- the same `startup_failure` "
            f"shape, just at the other end of the contract.",
        )


if __name__ == "__main__":
    unittest.main()
