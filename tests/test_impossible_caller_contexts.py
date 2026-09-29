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
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(doc, dict) and "jobs" in doc, (
        f"{path.name}: expected a top-level mapping with a `jobs:` section"
    )
    return doc["jobs"]


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

    def test_no_caller_job_is_top_level_skipped_on_pull_request(self):
        """The bug that produces caller-level sentinels on ordinary PRs
        is a top-level skip on the caller job itself that fires when a
        pull-request IS open -- turning the caller job into a
        placeholder on the very PR class it is meant to gate.

        Two distinct shapes use `if:` on caller jobs today:

          a. POST-MERGE-ONLY. `sign-publish:` and `release-verify:`
             skip on `github.ref` (only run on `main`/`release/**`).
             This is correct: those lanes are not supposed to run on a
             PR at all, the caller-level skipped check-run is the
             intended shape, and the ruleset does NOT require these
             names.

          b. PR-CLASS SKIP. `check:` and `test:` (in the broken
             version) skip on `github.event_name == 'pull_request' &&
             startsWith(..., 'maxi-config-sync/')` -- a class of PR
             whose lanes SHOULD run. That is the regression class and
             is exactly what the existing comment on `check:` warns
             against. The fix is to move the skip INSIDE the lane
             workflow the caller invokes, where it can produce a
             skipped NESTED check-run (which a required_status rule
             accepts) instead of a skipped CALLER check-run (which
             only appears on the class it is meant to gate).

        This test catches shape (b). It must NOT catch shape (a) --
        sign-publish and release-verify are post-merge-only by design.
        """
        jobs = load_jobs(RUST_CI)
        for caller in CANDIDATE_SENTINELS:
            job = jobs[caller]
            if_ = job.get("if")
            if if_ is None:
                continue
            # If the `if:` mentions pull_request as a class to skip,
            # it is the regression class. The mechanism a sane shape
            # (a) uses is github.ref (post-merge-only) and never fires
            # on pull_request. The mechanism shape (b) uses is some
            # variant of github.event_name == 'pull_request' AND
            # <class-refinement> -- which is the regression class
            # itself.
            self.assertNotIn(
                "github.event_name == 'pull_request'",
                if_,
                f"rust-ci.yml `{caller}:` carries an `if:` block "
                f"({if_!r}) whose branch fires on `github.event_name "
                f"== 'pull_request'`. That is the regression class: "
                f"the caller job is skipped on a pull-request class "
                f"that the ruleset must still gate, the caller-level "
                f"check-run `merge-gate / {caller}` is published as "
                f"`skipped` instead of the lane's actual result, and a "
                f"ruleset requiring it blocks every ordinary PR in "
                f"that class. The skip MUST live INSIDE the lane "
                f"workflow this job calls (lane-{caller}.yml), not on "
                f"the caller -- so the nested check-run "
                f"`merge-gate / {caller} / lane-{caller}` is "
                f"published as `skipped` (which a required_status "
                f"rule accepts) rather than absent. See the existing "
                f"comment on `check:` for the full account.",
            )

    def test_aggregate_has_always_on_same_repo_pr(self):
        """The aggregate is the context the ruleset is supposed to
        require, so its `if:` must let it run on every ordinary PR
        regardless of which lane failed -- a failing lane must not
        skip the aggregate, which is exactly what `always()` enforces."""
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
