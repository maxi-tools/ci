#!/usr/bin/env python3
"""Pin the same-repo fork guard on the self-check workflow.

The self-check workflow executes PR-controlled Python tests
(`tests/test_*.py`) and PR-controlled browser fixtures
(`tests/test_moments_gallery_browser.js`). On a fork pull request, the
PR head is not from `github.repository`, and the `GITHUB_TOKEN` is
read-only -- but the runner's own filesystem is not, so a fork PR that
ran on the org's self-hosted pool could plant code that the next
non-fork PR picks up (the "sleeper PR" attack on a shared cache).

This test pins the fork-isolation mechanism: a self-hosted job MUST
skip on a fork pull request. The `if:` shape it asserts is the same
shape every other self-hosted job in the org carries
(`github.event_name != 'pull_request' ||
github.event.pull_request.head.repo.full_name == github.repository`),
because push and workflow_dispatch carry no fork head. A future edit
that drops either half MUST fail this test.

The test also pins the self-hosted runner label, because the fork guard
is only meaningful when the runner is in the org's pool -- a hosted
runner escapes the guard by being a single-tenant ephemeral VM. The
shape `[self-hosted, Linux, X64]` is the lane the route-review-gate
shim picked for this repository; deviating from `[self-hosted]` is a
deliberate choice that must not happen without updating this test.

Run directly: `python3 tests/test_self_check_fork_guard.py`.
Run in CI: `self-check.yml`'s "Run the test suites" step.
"""
from __future__ import annotations

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github/workflows/self-check.yml"

FORK_GUARD_PHRASE = (
    "github.event.pull_request.head.repo.full_name == github.repository"
)
PUSH_GUARD_PHRASE = "github.event_name != 'pull_request'"


class SelfCheckForkGuardTest(unittest.TestCase):
    def setUp(self):
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or "jobs" not in doc:
            raise AssertionError(
                f"{WORKFLOW.name}: expected a top-level mapping with `jobs:`"
            )
        self.doc = doc
        self.job = doc["jobs"].get("self-check")
        self.assertIsNotNone(
            self.job,
            f"{WORKFLOW.name}: `self-check:` job missing.",
        )

    def test_job_carries_fork_guard(self):
        if_ = self.job.get("if") or ""
        self.assertIn(
            FORK_GUARD_PHRASE,
            if_,
            f"{WORKFLOW.name} `self-check:` `if:` does not carry the "
            f"same-repo fork guard. The job MUST skip on a fork pull "
            f"request so fork code never reaches the org's self-hosted "
            f"fleet. Every other self-hosted job in this file and in "
            f"`maxi-config` carries the same `if:` shape; a self-check "
            f"without it is the regression class.",
        )
        self.assertIn(
            PUSH_GUARD_PHRASE,
            if_,
            f"{WORKFLOW.name} `self-check:` `if:` does not exclude "
            f"`pull_request` for push/workflow_dispatch. The push half "
            f"of the guard keeps direct pushes and workflow_dispatch "
            f"runs on the self-hosted pool, where the same-repo check "
            f"would fail on a synthetic queue ref. A future edit that "
            f"drops the push half opens a regression on push events.",
        )

    def test_job_runs_on_self_hosted_lane(self):
        runs_on = self.job.get("runs-on")
        self.assertIsNotNone(
            runs_on,
            f"{WORKFLOW.name} `self-check:` `runs-on:` is missing.",
        )
        if isinstance(runs_on, list):
            runs_on_str = f"[{', '.join(runs_on)}]"
        else:
            runs_on_str = str(runs_on)
        self.assertIn(
            "self-hosted",
            runs_on_str,
            f"{WORKFLOW.name} `self-check:` `runs-on:` is {runs_on!r}. "
            f"The job MUST run on a self-hosted runner (and is fork-"
            f"guarded above); a hosted runner escapes the guard by "
            f"being a single-tenant ephemeral VM. The label "
            f"`[self-hosted, Linux, X64]` matches the lane the route-"
            f"review-gate shim picks for this repo.",
        )
        self.assertNotEqual(
            runs_on_str,
            "ubuntu-latest",
            f"{WORKFLOW.name} `self-check:` `runs-on:` is "
            f"`ubuntu-latest` -- a hosted runner. The Maxi rule "
            f"GitHub-hosted minutes = zero applies; the only "
            f"allowed hosted job is the external runner-freeze "
            f"watchdog. The fork guard above is meaningless on a "
            f"hosted runner.",
        )


if __name__ == "__main__":
    unittest.main()
