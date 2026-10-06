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

import ast
import pathlib
import unittest

import yaml

# The aggregate's `if:` evaluator's supported subset is string/number
# literals, `==`, `!=`, `&&`, `||`, `always()` (renamed `True` by the
# caller). `ast.literal_eval` would be the bandit-recommended
# alternative but the subset includes `and`/`or` -- Python operators
# `literal_eval` deliberately refuses -- so the supported grammar
# cannot be expressed through `literal_eval`. Instead, the caller
# already parsed the source with a fixed grammar (the token walker in
# CallerLevelContextsAreSentinelsTest:::renders_true) and rejects any
# node kind it does not recognise, so this evaluator only needs to
# evaluate the AST produced from a string the parser has already
# accepted. Rejecting any unexpected kind fails the test deliberately,
# which is the regression contract the bandit reference reasserts.
_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.Compare,
    ast.Constant, ast.And, ast.Or, ast.USub,
    ast.Eq, ast.NotEq,
)


def _safe_eval(expr: str) -> bool:
    tree = ast.parse(expr, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise ValueError(
                f"evaluator rejected {type(node).__name__}; the parser "
                "should have refused this node at the source. expr="
                f"{expr!r}"
            )
    # The AST walk above rejects every node kind the parser did not
    # produce. The supported subset (string/number literals,
    # `==`/`!=`, `&&`/`||`, `always()`) reduces to a fixed grammar
    # that bandit B307 / qlty cannot statically prove safe -- the
    # AST whitelist IS the proof. `ast.literal_eval` cannot express
    # this grammar because `and`/`or` are operators it refuses.
    # nosem: bandit.B307
    return bool(eval(compile(tree, "<renders_true>", "eval"), {"__builtins__": {}}))

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

        Evaluated, not substring-matched: the accepted spellings for
        the same-repo gate are `github.event_name == 'pull_request' &&
        <same-repo>` and the negated form `github.event_name !=
        'pull_request' || <same-repo>` (the #346 shape rule), and the
        merge-queue arm (`merge_group` admitted only with
        `checks_requested`) is spelled the negated way too. A
        substring pin would reject the accepted negated spelling or
        accept a semantically dead positive one, so the expression is
        rendered under real event scenarios and must come out TRUE for
        a same-repo pull_request, FALSE for a fork pull_request, and
        FALSE for every merge-queue lifecycle action.
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
        # TRUST IS STEP 1, NEVER A JOB-LEVEL `if:` (maxi-tools standing
        # rule; maxi-config#1028 verifier t_e5bf80a3 criterion c). The
        # job-level `if:` names the event scope plus `always()`; the
        # same-repo / queue-admission boundary is the `Trust admission`
        # step that opens the job and fails it RED for a fork PR or a
        # lifecycle action -- a job-level guard would publish `skipped`,
        # which an aggregate can be talked into reading as green.
        steps = jobs["merge-gate"].get("steps") or [{}]
        trust = steps[0]
        self.assertEqual(
            "trust-admission", trust.get("id"),
            "rust-ci.yml `merge-gate:` step 1 must be the trust admission "
            "step (never a job-level `if:` trust guard).",
        )
        trust_if = " ".join(str(trust.get("if", "")).split())
        prefix, suffix = "${{ !(", ") }}"
        self.assertTrue(
            trust_if.startswith(prefix) and trust_if.endswith(suffix),
            f"trust step if: must be a negated allowlist, got {trust_if!r}",
        )
        admitted_expr = trust_if[len(prefix):-len(suffix)].strip()
        self.assertIn(
            "github.event.pull_request.head.repo.full_name == github.repository",
            admitted_expr,
            "the trust step must admit pull requests only from this repository",
        )
        for term in ("head.repo.full_name", "github.event.action", "sender.id"):
            self.assertNotIn(
                term, if_,
                f"rust-ci.yml `merge-gate:` job-level if: carries trust term "
                f"{term!r}; move it to the trust admission step.",
            )

        def renders_true(if_: str, scenario: dict) -> bool:
            """Evaluate the aggregate's `if:` under one event scenario.

            GitHub expression subset: string/number literals, `!=`,
            `==`, `&&`, `||`, parentheses, `always()`. The bare `!`
            (logical NOT) is intentionally NOT in the supported
            subset: GitHub's `!` binds tighter than `==`, while Python's
            `not` binds looser, so a plain `!` → `not` translation would
            silently invert `!a == b` and emit a wrong verdict. The
            parser `self.fail`s on a bare `!` (cubic P3 review
            thread) so any future `if:` refactor that uses negated
            parentheses fails deliberately, with a readable error,
            instead of silently passing through `out.append('!')` and
            surfacing as a Python `SyntaxError` from the eval below.
            Context accesses resolve through `scenario`; an access
            that is not listed resolves to `''` (GitHub's behaviour
            for an absent event property, e.g. `github.event.action`
            on a push).
            """
            expr = " ".join(if_.split())
            expr = expr.replace("always()", "True")
            out, i = [], 0
            while i < len(expr):
                if expr[i] in "'\"":
                    quote = expr[i]
                    j = i + 1
                    while expr[j] != quote:
                        j += 1
                    out.append(repr(expr[i + 1:j]))
                    i = j + 1
                    continue
                if expr[i].isalpha() or expr[i] in "_.":
                    j = i
                    while j < len(expr) and (expr[j].isalnum() or expr[j] in "_."):
                        j += 1
                    term = expr[i:j]
                    if term in (
                        "and", "or", "True", "False",
                    ):
                        out.append(term)
                    elif term.startswith("github."):
                        out.append(repr(scenario.get(term, "")))
                    else:
                        self.fail(
                            f"rust-ci.yml `merge-gate:` if: references "
                            f"unknown term {term!r}; extend the evaluator "
                            f"deliberately rather than silently clearing it."
                        )
                    i = j
                    continue
                if expr[i] == "=" and expr[i + 1] == "=":
                    out.append("==")
                    i += 2
                    continue
                if expr[i] == "!" and expr[i + 1] == "=":
                    out.append("!=")
                    i += 2
                    continue
                if expr[i] == "!":
                    self.fail(
                        "rust-ci.yml `merge-gate:` if: contains a bare `!` "
                        "(logical NOT). The supported subset is `!=`, `==`, "
                        "`&&`, `||`, `always()`; bare `!` is rejected because "
                        "GitHub's `!` and Python's `not` differ in precedence "
                        "(GitHub binds tighter than `==`, Python binds looser) "
                        "and a literal translation would invert `!a == b`. "
                        "Refactor the expression to use Python `not` via the "
                        "supported `&&` / `||` disjuncts, or extend the "
                        "evaluator deliberately."
                    )
                if expr[i] == "&" and expr[i + 1] == "&":
                    out.append(" and ")
                    i += 2
                    continue
                if expr[i] == "|" and expr[i + 1] == "|":
                    out.append(" or ")
                    i += 2
                    continue
                out.append(expr[i])
                i += 1
            return _safe_eval("".join(out))

        repo = "maxi-tools/ci"
        scenarios = {
            "same-repo pull_request": {
                "github.event_name": "pull_request",
                "github.repository": repo,
                "github.event.pull_request.head.repo.full_name": repo,
            },
            "fork pull_request": {
                "github.event_name": "pull_request",
                "github.repository": repo,
                "github.event.pull_request.head.repo.full_name": "someone-else/fork",
            },
            "merge_group checks_requested": {
                "github.event_name": "merge_group",
                "github.repository": repo,
                "github.event.action": "checks_requested",
            },
            "merge_group created (lifecycle)": {
                "github.event_name": "merge_group",
                "github.repository": repo,
                "github.event.action": "created",
            },
            # The full lifecycle action surface, per the GitHub merge-queue
            # reference (the operator's review-gate thread on ci#83 asked
            # for these by name). A condition that admitted any of them
            # would mint a required-context run for an event no one
            # asked the verifier to evaluate, which is the exact regression
            # the queue arm exists to prevent.
            "merge_group merged (lifecycle)": {
                "github.event_name": "merge_group",
                "github.repository": repo,
                "github.event.action": "merged",
            },
            "merge_group pushed (lifecycle)": {
                "github.event_name": "merge_group",
                "github.repository": repo,
                "github.event.action": "pushed",
            },
            "merge_group removed (lifecycle)": {
                "github.event_name": "merge_group",
                "github.repository": repo,
                "github.event.action": "removed",
            },
            "merge_group deleted (lifecycle)": {
                "github.event_name": "merge_group",
                "github.repository": repo,
                "github.event.action": "deleted",
            },
            "push": {"github.event_name": "push", "github.repository": repo},
        }
        expectations = {
            "same-repo pull_request": True,
            "fork pull_request": False,
            # The queue arm admits checks_requested: the aggregate must
            # publish the required verdict on an admitted queue head.
            "merge_group checks_requested": True,
            # Lifecycle events must not mint a required-context run.
            "merge_group created (lifecycle)": False,
            "merge_group merged (lifecycle)": False,
            "merge_group pushed (lifecycle)": False,
            "merge_group removed (lifecycle)": False,
            "merge_group deleted (lifecycle)": False,
            "push": False,
        }
        def produces_verdict(scenario: dict) -> bool:
            """The job starts (job-level `if:`) AND its trust step admits
            the run. Anything else either never starts (push) or fails RED
            in step 1 (fork, lifecycle) -- neither publishes a verdict."""
            return renders_true(if_, scenario) and renders_true(
                admitted_expr, scenario)

        for name, scenario in scenarios.items():
            self.assertEqual(
                expectations[name],
                produces_verdict(scenario),
                f"rust-ci.yml `merge-gate:` if: ({' '.join(if_.split())!r}) "
                f"evaluates wrong under {name}; the aggregate must fire "
                f"exactly on same-repo pull_request heads and admitted "
                f"merge-queue groups.",
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
