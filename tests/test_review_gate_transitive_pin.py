#!/usr/bin/env python3
"""Pin the LOCAL review-gate caller at a reusable commit whose resolve-runner
pin already points at the queued-job-classified commit.

The route-review-gate shim (added in PR #98) reaches the corrected
`lane_job_estimates` ONLY through a chain:

  .github/workflows/review-gate.yml (the LOCAL caller)
    -> .github/workflows/review-gate-reusable.yml@<caller-pin>
    -> .github/actions/resolve-runner@<shim-pin>

The middle link was wrong in ci#98 at a4c559c7: the caller was pinned at
`09f330ce3a483a00604f9b386932c48052b81bdb` (the commit that adds the
classification), but the reusable at THAT sha pin had NOT yet advanced its
resolve-runner pin -- it still pointed at the previous `9a103abe...`
overflow commit, which is the one verifier t_77bfa3c0 REJECTed for
classifying queued jobs as zero. The head classifier lived in the
resolve-runner file at `09f330ce...`, but the live runtime call chain
reached the un-updated `9a103abe...` because the caller was one step
behind the upstream pin update.

The fix lives in two places: the caller advances to a sha whose
reusable's resolve-runner pin is `09f330ce...` or later, AND the test
that proves the chain is wired at the LOCAL caller exists. Without the
test, a future edit that re-targets the caller at an older reusable
sha for any reason (typo, cherry-pick miss, merge) silently restores
the bug, and tests/test_review_gate_route_shim.py -- which reads the
LOCAL reusable -- still passes because the LOCAL reusable at the head
IS correct.

This test follows the chain end-to-end:

  1. Parse .github/workflows/review-gate.yml, find the
     `uses: .../.github/workflows/review-gate-reusable.yml@<sha>` line.
  2. `git cat-file -p <sha>:.github/workflows/review-gate-reusable.yml`
     to read the reusable the caller actually invokes.
  3. Find the
     `uses: .../.github/actions/resolve-runner@<shim-sha>` line in the
     shim job's steps.
  4. Assert `shim-sha` is `09f330ce...` or a commit that descends from
     `09f330ce...` (the corrected lane_job_estimates commit). Reading
     the resolver action at `shim-sha` is one extra step the test does
     NOT take -- the pin itself is the contract.

Run directly: `python3 tests/test_review_gate_transitive_pin.py`.
Run in CI: `self-check.yml`'s "Run the test suites" step.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
CALLER = ROOT / ".github/workflows/review-gate.yml"
REUSABLE_PATH = ".github/workflows/review-gate-reusable.yml"
RESOLVE_RUNNER_PATH = ".github/actions/resolve-runner"

# The corrected resolver. Verifier t_77bfa3c0 REJECTed the prior commit
# (9a103abe...) for dropping queued jobs inside running workflows, and
# the fix landed at this commit. Any caller-pin whose shim-pin is at this
# sha or a descendant reaches the corrected lane_job_estimates; anything
# older reaches the defective version.
RESOLVER_PIN = "09f330ce3a483a00604f9b386932c48052b81bdb"

# Local caller pin must be at least this reusable commit; that commit
# is the first one on this branch whose reusable's resolve-runner pin
# advances to the corrected RESOLVER_PIN. Anchoring this in the test
# (rather than recomputing it from `git log -S`) means the test's
# expectation is one constant for an agent to grep, not a moving
# target that depends on the local checkout's history.
MIN_REUSABLE_PIN = "7ff1a52e026565293770eb9e9d8f01dec865b691"

PIN_RE = re.compile(
    r"maxi-tools/ci/" + re.escape(REUSABLE_PATH) + r"@([0-9a-f]{40})"
)
SHIM_PIN_RE = re.compile(
    r"maxi-tools/ci/" + re.escape(RESOLVE_RUNNER_PATH) + r"@([0-9a-f]{40})"
)


def _git_cat(commit: str, relpath: str) -> str:
    """Read `<relpath>` from the tree at `<commit>` in the local checkout.

    The action is intentionally specific to `git cat-file -p <commit>:<path>`.
    The runner image for this repo's self-check.yml has git installed, the
    checkout is full (not shallow), and the commit being read is a well-known
    ancestor of HEAD. The test refuses (rather than falls through) if the
    path is missing -- a missing file means the chain link is broken.
    """
    proc = subprocess.run(
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


def _is_descendant(child: str, ancestor: str) -> bool:
    """Return True iff `child` is `ancestor` or reachable from it.

    Uses `git merge-base --is-ancestor`, which exits 0 when the second
    argument is an ancestor of (or equal to) the first. The function
    wraps the exit code rather than parses its output, which is empty.
    """
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, child],
        cwd=str(ROOT),
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


class TransitivePinTest(unittest.TestCase):
    def setUp(self):
        self.caller_text = CALLER.read_text(encoding="utf-8")
        caller_match = PIN_RE.search(self.caller_text)
        if caller_match is None:
            self.assertIsNotNone(
                caller_match,
                f"{CALLER.name}: no `uses: ...{REUSABLE_PATH}@<40-char-sha>` "
                f"line found. The local reviewer pin must be a 40-char sha; "
                f"a 12-char short form, a tag, or a branch reference means "
                f"this test cannot follow the chain and the caller may be "
                f"silently executing an older runtime than the head source.",
            )
            raise AssertionError("unreachable")  # for type checkers
        self.caller_pin: str = caller_match.group(1)
        self.assertEqual(
            len(self.caller_pin), 40,
            f"{CALLER.name}: extracted pin {self.caller_pin!r} is not "
            f"40 chars; the regex matched but the chain link is malformed.",
        )

    def test_caller_pin_meets_minimum_reusable_commit(self):
        # The minimum is the commit on this branch whose reusable's
        # resolve-runner pin first advances to RESOLVER_PIN. Anchoring
        # the threshold in the test (rather than recomputing it from
        # history) makes a future regression obvious in CI: a caller
        # edit that drops the pin below the minimum fails this test,
        # even though tests/test_review_gate_route_shim.py still
        # passes (the local reusable is unchanged).
        self.assertTrue(
            _is_descendant(self.caller_pin, MIN_REUSABLE_PIN),
            f"{CALLER.name}: caller pin {self.caller_pin} is not a "
            f"descendant of {MIN_REUSABLE_PIN} (the first commit on "
            f"this branch whose reusable's resolve-runner pin advances "
            f"to the queued-job-classified commit). The local reviewer "
            f"call site will execute the older runtime than its head "
            f"source, restoring the t_77bfa3c0 defect end-to-end. "
            f"Advance the pin to {MIN_REUSABLE_PIN} or later.",
        )

    def test_reusable_at_caller_pin_pins_resolve_runner_at_or_after_correction(self):
        # Step 2 of the chain: the reusable at the caller pin must
        # itself pin the corrected resolver. This is the regression
        # the verifier caught at a4c559c7: the head reusable was
        # correct, but the LOCAL caller pointed at an OLDER reusable
        # commit whose resolve-runner pin was still the defective one.
        reusable_text = _git_cat(self.caller_pin, REUSABLE_PATH)
        shim_match = SHIM_PIN_RE.search(reusable_text)
        if shim_match is None:
            self.assertIsNotNone(
                shim_match,
                f"{REUSABLE_PATH} at {self.caller_pin}: no "
                f"`uses: ...{RESOLVE_RUNNER_PATH}@<40-char-sha>` line found. "
                f"The route-review-gate shim must SHA-pin resolve-runner; a "
                f"branch or tag reference means the route-review-gate shim's "
                f"actual implementation can drift without any test failing.",
            )
            raise AssertionError("unreachable")  # for type checkers
        shim_pin: str = shim_match.group(1)
        self.assertTrue(
            _is_descendant(shim_pin, RESOLVER_PIN),
            f"{REUSABLE_PATH} at {self.caller_pin}: shim's resolve-runner "
            f"pin is {shim_pin}, which is NOT a descendant of "
            f"{RESOLVER_PIN} (the queued-job-classified commit). The "
            f"route-review-gate shim will execute an older resolve-runner "
            f"than the one the head classifier was fixed in, restoring "
            f"the t_77bfa3c0 defect end-to-end. Advance the shim's pin "
            f"to {RESOLVER_PIN} or later.",
        )


if __name__ == "__main__":
    unittest.main()