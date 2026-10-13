#!/usr/bin/env python3
"""Pin the route-review-gate shim wiring in review-gate-reusable.yml.

The shim is the only path that makes the saturated-Linux → idle-Mac overflow
reachable from the public review-gate-reusable workflow. Without it, the four
downstream jobs hardcode `runs-on: fromJSON(inputs.runs_on)` and the overflow
mechanism in `.github/actions/resolve-runner/` is unreachable from this file.

This test fails closed on any of:

  1. `route-review-gate` is missing.
  2. The shim does not declare a fork guard. A shim that runs on a fork pull
     request puts fork code on the org's self-hosted fleet, which is the
     exact class of failure that every other self-hosted job in this file
     carries the same-repo `if:` to prevent. A future edit that drops the
     guard MUST fail this test.
  3. The shim does not call `./.github/actions/resolve-runner` with the
     documented lane. A shim that hardcodes a different lane (`macos-arm64`
     directly, say) would burn Mac runners on every PR regardless of
     saturation, defeating the purpose of the overflow mechanism.
  4. Any of the four downstream jobs (`supersede-check`, `invalidate`,
     `dedupe`, `gate`) does not list `route-review-gate` in its `needs:`.
     A downstream job that runs in parallel with the shim makes a fork
     PR's guard a no-op, because the shim's skip and the downstream's run
     are not coupled.
  5. Any of the four downstream jobs does not source its `runs-on` from
     the shim's output. A downstream job with a hardcoded `runs-on:` (or
     one that still reads `fromJSON(inputs.runs_on)`) bypasses the router
     entirely; the overflow never reaches it.

Run directly: `python3 tests/test_review_gate_route_shim.py`.
Run in CI: `self-check.yml`'s "Run the test suites" step.
"""
from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/review-gate-reusable.yml"

DOWNSTREAM_JOBS = ("supersede-check", "invalidate", "dedupe", "gate")
SHIM_NAME = "route-review-gate"
EXPECTED_LANE = "linux-arm64"
EXPECTED_REPO = "ci"
# The heaviest downstream job; see the comment on the resolve-runner step
# in the shim. Picking a lighter job would mis-size the overflow answer
# for `gate`, which is what carries the verdict the ruleset requires.
EXPECTED_JOB = "gate"


class RouteShimWiringTest(unittest.TestCase):
    def setUp(self):
        doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or "jobs" not in doc:
            raise AssertionError(
                f"{WORKFLOW.name}: expected a top-level mapping with `jobs:`"
            )
        self.doc = doc
        self.jobs = doc["jobs"]

    def test_shim_job_exists(self):
        self.assertIn(
            SHIM_NAME,
            self.jobs,
            f"{WORKFLOW.name} does not declare a `{SHIM_NAME}:` job. "
            f"The saturated-Linux → idle-Mac overflow is unreachable from "
            f"this reusable without the shim; the four downstream jobs "
            f"hardcode `fromJSON(inputs.runs_on)` and cannot read "
            f"resolve-runner outputs on their own.",
        )

    def test_shim_has_fork_guard(self):
        shim = self.jobs[SHIM_NAME]
        if_ = shim.get("if") or ""
        # Pin both halves of the trust guard. A job-level `if:` that omits
        # the same-repo clause is the regression class on its own: a fork
        # PR whose `pull_request.head.repo.full_name != github.repository`
        # would still reach the shim, run resolve-runner, and put the
        # `route-*` step on a self-hosted runner. A job-level `if:` that
        # mentions the phrase but not `head.repo` is the same regression
        # under a different spelling.
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            if_,
            f"{WORKFLOW.name} `{SHIM_NAME}:` carries no same-repo fork guard. "
            f"The shim MUST skip on a fork pull request so fork code never "
            f"reaches the org's self-hosted fleet. Every other self-hosted "
            f"job in this file carries the same gate; a shim without it is "
            f"the regression class.",
        )
        # merge_group carries no `pull_request` payload, so the same-repo
        # check above would raise on a synthetic queue ref. The other
        # self-hosted jobs in this file (supersede-check, invalidate,
        # dedupe, gate) all guard with `github.event_name != 'merge_group'`
        # for that reason; a shim that does not match skips the four
        # downstream jobs on merge_group via the `needs:` chain, but the
        # SHIM itself would still try to run, hit a missing `pull_request`
        # field, and fail its `if:`. Mirror the same phrase.
        self.assertIn(
            "github.event_name != 'merge_group'",
            if_,
            f"{WORKFLOW.name} `{SHIM_NAME}:` `if:` does not exclude "
            f"`merge_group`. The synthetic queue ref carries no "
            f"`pull_request` payload, so the same-repo check would error "
            f"on a queue ref and the shim would fail before its "
            f"`needs:`-chain skip can take effect. Every other "
            f"self-hosted job in this file carries the same `merge_group` "
            f"guard; a shim without it is the regression class.",
        )

    def test_shim_invokes_resolve_runner_with_documented_inputs(self):
        shim = self.jobs[SHIM_NAME]
        steps = shim.get("steps") or []
        resolve_steps = [
            s for s in steps
            if isinstance(s.get("uses"), str)
            and s["uses"].endswith("./.github/actions/resolve-runner")
        ]
        self.assertEqual(
            len(resolve_steps), 1,
            f"{WORKFLOW.name} `{SHIM_NAME}:` must invoke exactly one "
            f"`./.github/actions/resolve-runner` step; found {len(resolve_steps)}. "
            f"Multiple invocations race on the same output and a missing "
            f"invocation leaves the downstream jobs reading an empty output.",
        )
        with_ = resolve_steps[0].get("with") or {}
        self.assertEqual(
            with_.get("lane"), EXPECTED_LANE,
            f"{WORKFLOW.name} `{SHIM_NAME}:` resolve-runner `lane:` is "
            f"{with_.get('lane')!r}; expected {EXPECTED_LANE!r}. A shim that "
            f"picks Mac first (`macos-arm64`) burns Mac runners on every PR "
            f"regardless of saturation and defeats the overflow's whole "
            f"purpose; a shim that picks a lane without an idle policy "
            f"definition falls back to defaults and the overflow is never "
            f"triggered.",
        )
        self.assertEqual(
            with_.get("repo"), EXPECTED_REPO,
            f"{WORKFLOW.name} `{SHIM_NAME}:` resolve-runner `repo:` is "
            f"{with_.get('repo')!r}; expected {EXPECTED_REPO!r}. The router "
            f"reads `[repos.<repo>.jobs.<job>]` in `ci/runner-routing.toml`; "
            f"a wrong repo key means the policy entry (added in PR-C in "
            f"maxi-config) is never matched.",
        )
        self.assertEqual(
            with_.get("job"), EXPECTED_JOB,
            f"{WORKFLOW.name} `{SHIM_NAME}:` resolve-runner `job:` is "
            f"{with_.get('job')!r}; expected {EXPECTED_JOB!r}. The four "
            f"downstream jobs share the same overflow answer; the shim MUST "
            f"size against the heaviest (`gate`) so the queue_minutes "
            f"estimate is correct.",
        )

    def test_every_downstream_job_needs_shim(self):
        for name in DOWNSTREAM_JOBS:
            job = self.jobs[name]
            needs = job.get("needs")
            # `needs:` may be a string (one job) or a list. Normalise.
            needs_set = {needs} if isinstance(needs, str) else set(needs or [])
            self.assertIn(
                SHIM_NAME, needs_set,
                f"{WORKFLOW.name} `{name}:` does not list `{SHIM_NAME}` in "
                f"its `needs:` ({needs!r}). A downstream job that runs in "
                f"parallel with the shim decouples the fork guard from the "
                f"downstream; a fork PR's shim skip does not propagate and "
                f"fork code reaches the org's self-hosted fleet.",
            )

    def test_every_downstream_job_runs_on_shim_output(self):
        # The downstream jobs must read the shim's `outputs.runs_on` via
        # `fromJSON(needs.route-review-gate.outputs.runs_on)`. A hardcoded
        # `runs-on:` (or one that still reads `inputs.runs_on`) bypasses
        # the router entirely.
        pattern = re.compile(
            r"fromJSON\(\s*needs\." + re.escape(SHIM_NAME) + r"\.outputs\.runs_on\s*\)"
        )
        for name in DOWNSTREAM_JOBS:
            job = self.jobs[name]
            runs_on = job.get("runs-on") or ""
            self.assertRegex(
                runs_on,
                pattern,
                f"{WORKFLOW.name} `{name}:` `runs-on:` is {runs_on!r}. The "
                f"downstream job MUST read `fromJSON(needs.{SHIM_NAME}."
                f"outputs.runs_on)` so the router's answer reaches it. A "
                f"hardcoded `runs-on:` (or one that still reads "
                f"`inputs.runs_on`) bypasses the router entirely; the "
                f"overflow never reaches this job.",
            )

    def test_shim_checks_out_before_using_local_actions(self):
        # The shim's resolve-runner and fetch-policy steps use the local
        # `./.github/actions/...` path, which is only on disk after
        # `actions/checkout@v4` has run. Without the checkout, the runner
        # reports `Can't find 'action.yml'...Did you forget to run
        # actions/checkout before running your local action?` and the
        # shim fails before it can resolve anything. The reusable runs
        # in a context with NO checkout of the calling repo (this file's
        # owning repo, maxi-tools/ci), so a checkout is required
        # explicitly. Mirrors `route-clippy` in
        # maxi-config/.github/workflows/clippy.yml.
        shim = self.jobs[SHIM_NAME]
        steps = shim.get("steps") or []
        step_uses = [s.get("uses") for s in steps if isinstance(s, dict)]
        self.assertTrue(
            any(isinstance(u, str) and u.startswith("actions/checkout@") for u in step_uses),
            f"{WORKFLOW.name} `{SHIM_NAME}:` does not call "
            f"`actions/checkout@*` before its `uses: ./.github/actions/...` "
            f"steps. The local action bodies are not on disk in a "
            f"reusable's runner context without an explicit checkout, and "
            f"the resolve-runner step errors with `Can't find 'action.yml'"
            f"...` before producing a runs-on output. Mirrors the "
            f"`route-clippy` shim in maxi-config.",
        )

    def test_shim_runs_on_inputs_runs_on(self):
        # The shim's OWN runner is the caller's `inputs.runs_on`. This
        # is what lets a caller that wants hosted runners (e.g. the
        # local `review-gate.yml` here) keep the shim on hosted even
        # though the downstream jobs land on the router's answer.
        # The shim does NOT run on the router's output -- that would
        # create a circular dependency.
        shim = self.jobs[SHIM_NAME]
        runs_on = shim.get("runs-on") or ""
        self.assertIn(
            "inputs.runs_on",
            runs_on,
            f"{WORKFLOW.name} `{SHIM_NAME}:` `runs-on:` is {runs_on!r}. The "
            f"shim's OWN runner is the caller's `inputs.runs_on`, NOT the "
            f"router's output (a circular dependency would deadlock the "
            f"workflow).",
        )
        self.assertNotIn(
            f"needs.{SHIM_NAME}",
            runs_on,
            f"{WORKFLOW.name} `{SHIM_NAME}:` `runs-on:` reads "
            f"`needs.{SHIM_NAME}.*` -- a job cannot depend on its own "
            f"outputs, and an actionlint error will fire before this test "
            f"can.",
        )


if __name__ == "__main__":
    unittest.main()
