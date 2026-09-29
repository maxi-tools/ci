#!/usr/bin/env python3
"""Pin the caller-context contract that fleet rulesets depend on.

When `rust-ci.yml` is invoked through `workflow_call`, GitHub publishes a
check-run for every job whose own reusable-workflow call is reached. The
NAMING shape depends on what we ship here, not on the consumer's
`.github/workflows/ci.yml`:

  Caller job RUNS, callee job RUNS    ->  `merge-gate / <caller> / <callee>`
                                          with conclusion success/failure
                                          (and `merge-gate / <callee>`
                                           ABSENT)
  Caller job RUNS, callee job SKIPS   ->  `merge-gate / <caller> / <callee>`
                                          with conclusion skipped
  Caller job ITSELF SKIPS             ->  `merge-gate / <caller>`
                                          with conclusion skipped
                                          AND `merge-gate / <caller> / <callee>`
                                          is ABSENT

That means the caller-level name `merge-gate / check` is published as a
SENTINEL ONLY -- it appears if and only if the caller job `check:`
itself is skipped, which is not the normal case. On an ordinary PR (lanes
run) it is ABSENT; on a fan-out PR (caller skipped) it is present but
skipped, which GitHub treats as satisfied. The fleet's required_status
rulesets need a context that is published SUCCESS-or-SKIPPED on every PR
whose lane chain reaches the merge gate. That context is one of:

  `merge-gate / merge-gate` (the aggregate)
  `merge-gate / <caller> / <callee>` (the nested forms)

The caller-level forms (`merge-gate / plan`, `merge-gate / check`,
`merge-gate / test`, `merge-gate / package`, `merge-gate / sign-publish`,
`merge-gate / release-verify`) are SENTINELS, not verdicts. A ruleset
that requires one of them blocks every ordinary PR whose lanes actually
run, which is exactly the regression maxi-config#763 / t_0c24f236 caused
on 2026-09-20 (rolled out to 31 repos for ~75 minutes before being
reverted).

This test pins two things at the source so the regression cannot return
through a refactor of `rust-ci.yml`:

  1. The set of caller job names in `rust-ci.yml` is exactly the
     documented set. A future lane added by name (e.g. `audit:`) MUST be
     listed in CANDIDATE_SENTINELS -- otherwise this test fails and the
     reviewer is forced to decide whether the new caller name is itself
     the regression class.

  2. None of the six caller-level names is *also* a published
     check-run with success/failure on an ordinary PR. The way that
     could happen in this repository is for a job in `rust-ci.yml` to
     gain an `if:` block that skips it on ordinary PRs (rather than
     skipping INSIDE the lane it calls) -- which is precisely the bug
     the existing `check:` and `test:` comments warn against. The test
     reads the `if:` of every caller job in `rust-ci.yml` and asserts
     there is no top-level skip pattern that would turn the caller into
     a placeholder.

Both checks fail closed: a workflow refactor that breaks either rule
fails this test before it can ship.

Run directly: `python3 tests/test_impossible_caller_contexts.py`.
Run in CI: `self-check.yml`'s "Run the test suites" step.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUST_CI = ROOT / ".github/workflows/rust-ci.yml"

# Jobs declared in `rust-ci.yml` whose CALLER-level check-run
# `merge-gate / <job>` is a sentinel-only context: published as `skipped`
# when the caller job itself is skipped, and ABSENT on every PR whose
# lane chain reaches the merge gate. A ruleset that requires any of
# these blocks ordinary PRs. The list MUST stay in sync with the jobs
# declared in rust-ci.yml; if a new lane is added, the test will fail
# until the reviewer decides the new caller name is correctly classified.
#
# `merge-gate` itself is intentionally NOT in this set: the aggregate
# is what the ruleset is supposed to require.
CANDIDATE_SENTINELS = frozenset(
    {
        "plan",
        "check",
        "test",
        "package",
        "sign-publish",
        "release-verify",
    }
)


def load_jobs(path: pathlib.Path) -> dict:
    """Load the `jobs:` section of a workflow file as a dict.

    A helper for the contract tests; raises ValueError if the file
    is not a workflow-shaped YAML mapping.
    """
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "jobs" not in doc:
        raise ValueError(
            f"{path.name}: expected a top-level mapping with a `jobs:` section"
        )
    return doc["jobs"]


# Per-caller reusable-workflow target. A caller job in rust-ci.yml that
# stops calling its lane -- by losing its `uses:` directive and
# becoming a `runs-on:` + `steps:` job -- no longer publishes the
# nested check-run the ruleset requires by name. The job name still
# matches the regression-class set, so the name-only assertion would
# pass on the broken shape. Pin each caller's `uses:` target so a
# caller converted to inline steps fails this test.
#
# `merge-gate` is intentionally absent -- it is the aggregate, not a
# caller, and runs inline rather than calling a reusable workflow.
CALLER_USES_TARGETS = frozenset(
    {
        f"./.github/workflows/lane-{name}.yml"
        for name in (
            "plan",
            "check",
            "test",
            "package",
            "sign-publish",
            "release-verify",
        )
    }
)


class RustCiJobsContractTest(unittest.TestCase):
    """The set of caller job names in rust-ci.yml MUST match the
    documented set, otherwise the review above cannot recognise a new
    caller-level name as the regression class it actually is."""

    def test_every_caller_job_is_a_documented_sentinel(self):
        jobs = load_jobs(RUST_CI)
        self.assertEqual(
            set(jobs) - {"merge-gate"},
            CANDIDATE_SENTINELS,
            "rust-ci.yml declares caller jobs that are not in "
            "CANDIDATE_SENTINELS (or vice versa). A new lane MUST be "
            "added to CANDIDATE_SENTINELS so the regression class is "
            "still recognised. If a caller-level job is intentionally "
            "NOT a sentinel (it publishes its own verdict, not a "
            "placeholder), document that here and remove it from "
            "CANDIDATE_SENTINELS only with the reviewer's sign-off.",
        )

    def test_every_caller_job_calls_its_lane_reusable(self):
        """Pin the `uses:` target of each caller job so a caller
        converted to inline `runs-on:` + `steps:` -- which would lose
        the nested check-run the ruleset requires by name -- fails
        this test rather than silently passing the name assertion."""
        jobs = load_jobs(RUST_CI)
        for caller in CANDIDATE_SENTINELS:
            job = jobs[caller]
            uses = job.get("uses")
            self.assertIn(
                uses,
                CALLER_USES_TARGETS,
                f"rust-ci.yml `{caller}:` does not call its lane "
                f"reusable workflow. Got `uses: {uses!r}`; expected "
                f"one of {sorted(CALLER_USES_TARGETS)}. The caller "
                f"job MUST invoke `./.github/workflows/lane-{caller}.yml` "
                f"so the nested check-run `merge-gate / {caller} / "
                f"lane-{caller}` is published for the ruleset to read. "
                f"An inline `runs-on:` + `steps:` job loses that "
                f"check-run name even when its job name is unchanged, "
                f"and the regression class returns. See the comment "
                f"on `check:` and `test:` for the full account.",
            )

    def test_no_caller_job_is_top_level_skipped_on_pull_request(self):
        """The bug that produces caller-level sentinels on ordinary PRs
        is a top-level skip on the caller job itself that fires when a
        pull-request IS open -- turning the caller job into a
        placeholder on the very PR class it is meant to gate.

        Three patterns are PERMITTED on a caller `if:` today:

          a. POST-MERGE-ONLY. `sign-publish:` and `release-verify:`
             skip on `github.ref` (only run on `main`/`release/**`).
             This is correct: those lanes are not supposed to run on
             a PR at all, the caller-level skipped check-run is the
             intended shape, and the ruleset does NOT require these
             names.

          b. SAME-REPO GATE. `plan:` narrows the PR class to same-repo
             PRs via `head.repo.full_name == github.repository`,
             explicitly excluding fork PRs from the self-hosted
             fleet. The caller still runs on every same-repo PR, so
             the nested check-run `merge-gate / plan / lane-plan` is
             published as success/failure on the class the ruleset
             must gate.

          c. LANE-RESULT DEPENDENCY. `package:` runs only when its
             upstream lanes succeeded (`needs.plan.result == 'success'
             && (needs.check.result in {success, skipped}) && ...`).
             This is a topological guard, not a class guard, and does
             not narrow the PR class.

        Any other reference to `github.event_name` -- e.g.
        `github.event_name == 'pull_request' && startsWith(..., ...)`,
        or a fan-out guard, or a date guard, or a docs-only guard on
        the caller -- is the regression class, because it narrows the
        PR class in a way the ruleset does not understand.
        """
        jobs = load_jobs(RUST_CI)
        for caller in CANDIDATE_SENTINELS:
            job = jobs[caller]
            if_ = job.get("if")
            if if_ is None:
                continue
            mentions_event_name = "github.event_name" in if_
            if not mentions_event_name:
                # Without `github.event_name`, two patterns are
                # permitted: post-merge-only (a) and lane-result
                # dependency (c). A pattern that references neither
                # `github.ref` (post-merge-only) nor `needs.` (lane
                # dependency) -- e.g. a fan-out guard, a docs-only
                # guard, a date guard, a fork guard that does not
                # mention the repo -- is the regression class.
                self.assertTrue(
                    "github.ref" in if_ or "needs." in if_,
                    f"rust-ci.yml `{caller}:` carries an `if:` block "
                    f"({if_!r}) that references neither "
                    f"`github.event_name` (the regression class when "
                    f"absent of the same-repo clause) nor "
                    f"`github.ref` (the post-merge-only shape) nor "
                    f"`needs.` (the lane-result dependency shape). "
                    f"If a caller carries an `if:` at all, it must be "
                    f"one of the three permitted shapes -- post-"
                    f"merge-only, same-repo gate, or lane-result "
                    f"dependency. Any other shape (a fan-out guard, "
                    f"a fork guard, a docs-only guard, a date guard, "
                    f"etc.) belongs INSIDE the lane workflow this "
                    f"job calls, not on the caller.",
                )
                continue
            # The `if:` references `github.event_name`. The only
            # legitimate use on a caller is the same-repo gate (b):
            # the literal phrase
            # `head.repo.full_name == github.repository`
            # distinguishes it from the regression class. Any other
            # reference -- a fan-out guard, a docs-only guard, a
            # date guard, a fork guard that does not name the repo --
            # is the regression class.
            self.assertIn(
                "head.repo.full_name == github.repository",
                if_,
                f"rust-ci.yml `{caller}:` carries an `if:` block "
                f"({if_!r}) that references `github.event_name` "
                f"without gating on "
                f"`github.event.pull_request.head.repo.full_name == "
                f"github.repository`. That is the regression class: "
                f"the caller is skipped on a pull-request class that "
                f"the ruleset must still gate, the caller-level "
                f"check-run `merge-gate / {caller}` is published as "
                f"`skipped` instead of the lane's actual result, and a "
                f"ruleset requiring it blocks every ordinary PR in "
                f"that class. The only legitimate use of "
                f"`github.event_name` on a caller is the same-repo "
                f"gate (above). Any other shape -- a fan-out guard, "
                f"a docs-only guard, a date guard, a fork guard that "
                f"does not name the repo -- belongs INSIDE the lane "
                f"workflow this job calls (lane-{caller}.yml), not "
                f"on the caller. See the existing comment on "
                f"`check:` for the full account.",
            )

    def test_aggregate_has_always_on_same_repo_pr(self):
        """The aggregate is the context the ruleset is supposed to
        require, so its `if:` must let it run on every same-repo PR
        regardless of which lane failed.

        The rule has two halves that BOTH have to hold:

          a. `always()` -- a failing lane must not skip the aggregate.
             Without `always()`, a red upstream lane cascades into a
             skipped aggregate, which the ruleset reads as "no
             verdict", which is read as not red; the regression class
             then returns through a different path.

          b. SAME-REPO PULL_REQUEST COVERAGE -- the aggregate must
             actually fire when a same-repo PR is open. `always()`
             alone does not satisfy this: an expression like
             `always() && github.event_name == 'push'` has `always()`
             but skips every PR class entirely. Pin both halves.
        """
        jobs = load_jobs(RUST_CI)
        if_ = jobs["merge-gate"].get("if")
        self.assertIsNotNone(
            if_,
            "rust-ci.yml `merge-gate:` has no `if:`; the aggregate "
            "must run on every same-repo PR regardless of lane "
            "outcome, otherwise a red lane skips the verdict.",
        )
        self.assertIn(
            "always()",
            if_,
            f"rust-ci.yml `merge-gate:` if: ({if_!r}) does not include "
            f"`always()`. A failing lane must NOT skip the aggregate; "
            f"the ruleset reads `merge-gate / merge-gate` to decide "
            f"whether to block, and a missing conclusion is read as "
            f"no verdict, not red.",
        )
        self.assertIn(
            "github.event_name == 'pull_request'",
            if_,
            f"rust-ci.yml `merge-gate:` if: ({if_!r}) does not "
            f"reference `github.event_name == 'pull_request'`. "
            f"`always()` alone is not enough -- an expression like "
            f"`always() && github.event_name == 'push'` skips every "
            f"PR class entirely, the aggregate check-run is then "
            f"absent on the PR class the ruleset is supposed to gate, "
            f"and a ruleset requiring `merge-gate / merge-gate` "
            f"blocks every same-repo PR. The aggregate must fire on "
            f"`github.event_name == 'pull_request'` (and any further "
            f"refinement -- same-repo head, fork bypass, etc. -- "
            f"must still leave that class passing).",
        )
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            if_,
            f"rust-ci.yml `merge-gate:` if: ({if_!r}) does not gate "
            f"on `github.event.pull_request.head.repo.full_name == "
            f"github.repository`. The aggregate must run on same-repo "
            f"PRs and skip on fork PRs (fork code must not reach the "
            f"self-hosted fleet). A condition that lacks this clause "
            f"either skips legitimate PRs or runs on forks, both of "
            f"which are the regression class.",
        )


class CallerLevelContextsAreSentinelsTest(unittest.TestCase):
    """The exact regression that maxi-config#763 / t_0c24f236 caused:

    a fleet ruleset required `merge-gate / check` and `merge-gate /
    test`, which a caller that ACTUALLY CALLS the reusable workflow
    never emits as a non-skipped conclusion. The full set of
    caller-level names the ruleset must never require is the set
    declared in `rust-ci.yml` -- not a curated subset, because a
    partial subset still misses the next caller the fleet adds."""

    def test_no_caller_level_name_is_in_a_designed_state(self):
        """The test asserts the contract directly so that the next
        refactor cannot quietly introduce a caller-level name that
        looks safe but is the regression in disguise."""
        jobs = load_jobs(RUST_CI)
        caller_names = {f"merge-gate / {job}" for job in jobs if job != "merge-gate"}
        # The aggregate itself is what the ruleset is supposed to
        # require -- this test names the regression class, not the
        # designed state. Caller-level names that pair with an actual
        # success/failure verdict DO NOT EXIST in this workflow; if
        # that ever changes, this assertion is what blocks it.
        for name in caller_names:
            self.assertIn(
                name.removesuffix(" / merge-gate").removeprefix("merge-gate / "),
                CANDIDATE_SENTINELS,
                f"rust-ci.yml declares a caller job whose caller-level "
                f"name {name!r} is not in CANDIDATE_SENTINELS. Add it "
                f"or rename the job so the regression class is "
                f"recognised at the source.",
            )

    def test_sentinel_set_is_nonempty_and_canonical(self):
        """A future lane added without being listed in
        CANDIDATE_SENTINELS is itself the regression. Empty would
        mean rust-ci.yml has no caller jobs at all -- which would
        also mean the aggregate has nothing to gate, and the test
        `test_every_caller_job_is_a_documented_sentinel` already fails
        first."""
        self.assertGreater(
            len(CANDIDATE_SENTINELS),
            0,
            "CANDIDATE_SENTINELS is empty: rust-ci.yml has no caller "
            "jobs to be checked.",
        )
        self.assertNotIn(
            "merge-gate",
            CANDIDATE_SENTINELS,
            "CANDIDATE_SENTINELS must not contain 'merge-gate': the "
            "aggregate is the verdict, not a sentinel.",
        )


if __name__ == "__main__":
    unittest.main()
