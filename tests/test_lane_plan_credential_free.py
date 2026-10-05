#!/usr/bin/env python3
"""The credential-free plan lane, RUN not read.

`lane-plan.yml` is the router every downstream lane is SKIPPED behind: `plan:`
in `rust-ci.yml` gates `check`, `test`, `package`, `sign-publish` and
`release-verify` on `needs.plan.result == 'success'`, and the required
`merge-gate / merge-gate` aggregate reads the same chain. So when a caller has
no App credentials and the plan lane fails, the entire repository's CI is
skipped and the merge gate sees no verdict -- reported as a red gate on a
change that is perfectly fine.

That is not hypothetical. maxi-config#1028 made `APP_ID` / `APP_PRIVATE_KEY`
optional on the caller so forks and public-only consumers could run the chain,
and codex's P1 review thread on that PR (PRRT_kwDORx_z3s6o0yEN) pointed out the
plan lane still declared them `required: true` and ran
`actions/create-github-app-token` unconditionally -- so the advertised
credential-free mode did not work through the composer that was supposed to
provide it.

The fix landed in ci (d05f593, 2c64498, 124c4fe) with no test pinning it, and
the drift stayed invisible on the consumer side because `lane-plan.yml` is not
in maxi-config's `test_public_ci_copy_agrees.py` PAIRS/RIDE_ALONG maps, so its
vendored bytes are unpinned. A refactor that drops the credential gate, or
over-gates the wrong steps, would reintroduce the fleet-wide skip with nothing
red. These tests are that something red.

Two halves, deliberately:

  EXECUTION tests run the steps' own `run:` bodies in bash and read the
  outputs they write to `$GITHUB_OUTPUT`, so the thing under test is the bytes
  CI runs rather than a restatement of them.

  SHAPE tests read the workflow structure, for the properties that live in
  `if:` and `secrets:` where there is nothing to execute. Both are needed:
  actionlint checks that the workflow parses, not what its shell decides, and
  it does not evaluate a `steps.<id>.outputs` reference against step order at
  all.

Run directly: `python3 tests/test_lane_plan_credential_free.py`.
Run in CI: `self-check.yml`'s "Run the test suites" step.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LANE_PLAN = ROOT / ".github/workflows/lane-plan.yml"
RUST_CI = ROOT / ".github/workflows/rust-ci.yml"

JOB = "lane-plan"

# The App secrets. The contract under test is that a caller MAY omit both, so
# these names are the subject of the shape tests and must not drift silently.
APP_SECRETS = ("APP_ID", "APP_PRIVATE_KEY")

# Steps that CANNOT run without an App token, because each one either mints
# the token or consumes it, or reads a path that only the token's sparse
# checkout of the private policy repo creates. Every one of these must carry the
# credential gate; a step that loses it is the original defect.
#
#   Generate org repo token      -- mints; dies in ~5s on an empty app-id
#   Checkout maxi-config policy  -- consumes the token; the private repo 404s
#   Resolve runner               -- consumes the token for the org-runner probe
#   Plan packaging impact        -- reads .maxi-config/scripts/, which only the
#                                   policy checkout above creates
CREDENTIAL_DEPENDENT_STEPS = (
    "Generate org repo token",
    "Checkout maxi-config policy",
    "Resolve runner",
    "Plan packaging impact",
)

# Steps that MUST NOT be gated on credentials, and the reason each one is a
# trap when it is.
#
# Both authenticate with the CALLER's own `github.token`, which GitHub always
# mints for the job -- it is the App token that is absent in credential-free
# mode, not the job's own token. So both work fine with no App credentials,
# and both produce the verdict the merge gate reads.
#
# The failure mode is the natural-looking "fix": gating these on
# `has_credentials` for tidiness or symmetry with the steps above. That does
# not fail loudly. `scope` then falls through to the fallback's `scope=full`,
# so every docs-only change from a credential-free caller stops being
# `merge-gate-only` and starts running the full check/test chain -- the lane
# gets slower and more expensive for exactly the PRs the scope classifier
# exists to make cheap, and nothing in CI reports it as a defect.
#
# Pinned here so the next person to tidy the `if:` block finds a red test.
CREDENTIAL_INDEPENDENT_STEPS = (
    "Collect changed files",
    "Classify change scope",
)

# The job outputs the merge gate and the sibling lanes read. In credential-free
# mode none of them may be empty: an empty output is what a caller reads as
# "the plan did not decide", and `scope` in particular must stay one of the two
# values the aggregate's skip-waiver recognises.
JOB_OUTPUTS = (
    "runs_on",
    "provider",
    "packaging_impact",
    "packaging_reason",
    "scope",
    "scope_reason",
)

# The keys the credential-free fallback step writes to `$GITHUB_OUTPUT`. This
# is NOT the same tuple as JOB_OUTPUTS and the difference is load-bearing: the
# runner label is spelled `runs-on` at the step boundary (matching the
# resolve-runner action's own output) and `runs_on` at the job boundary
# (matching `workflow_call.outputs`, where a hyphen is not legal in an output
# name). Each job output bridges the two, e.g.
#   runs_on: ${{ ... || steps.credentials-fallback.outputs.runs-on }}
# so a test that conflates the two vocabularies reads a correctly-filled step
# as an empty output. `test_every_step_output_reaches_a_job_output` is what
# keeps the bridge intact.
FALLBACK_STEP_OUTPUTS = (
    "runs-on",
    "provider",
    "packaging_impact",
    "packaging_reason",
    "scope",
    "scope_reason",
)


def load_lane_plan() -> dict:
    """The parsed lane-plan workflow.

    Raises rather than returning a default, so a workflow that fails to parse
    fails the suite instead of silently testing nothing.
    """
    doc = yaml.safe_load(LANE_PLAN.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "jobs" not in doc:
        raise ValueError(
            f"{LANE_PLAN.name}: expected a top-level mapping with a `jobs:` "
            "section; a rename of the file or the job must not silently stop "
            "testing the credential-free contract"
        )
    return doc


def workflow_call() -> dict:
    """The `workflow_call:` block, whatever YAML made of the bare `on:` key.

    PyYAML implements YAML 1.1, where a bare `on:` is the boolean true, so the
    key parses as `True` and `doc["on"]` raises KeyError. GitHub's own parser
    treats it as the string "on". Handle both spellings so this helper is not
    the thing that breaks if the file is ever read by a 1.2 parser instead.
    """
    doc = load_lane_plan()
    for key in ("on", True):
        if key in doc and "workflow_call" in doc[key]:
            return doc[key]["workflow_call"]
    raise ValueError(
        f"{LANE_PLAN.name}: no `on.workflow_call` block; this workflow is a "
        "reusable lane and the credential-free contract is declared on its "
        "workflow_call inputs and secrets"
    )


def steps() -> list:
    """The lane-plan job's steps, in file order.

    Order is load-bearing twice over: a step's `if:` may only reference an
    output of a step that already ran, and the token mint must precede every
    consumer of `steps.app-token.outputs.token`.
    """
    return load_lane_plan()["jobs"][JOB]["steps"]


def step_by_name(name: str) -> dict:
    """One step, or fail loudly.

    A missing step is a rename or a deletion, and both are exactly the kind of
    change that should stop this suite from quietly testing less than it did.
    """
    for step in steps():
        if step.get("name") == name:
            return step
    raise AssertionError(
        f"lane-plan.yml has no step named {name!r}; this test reads that step "
        "directly, so a rename must not silently stop testing it"
    )


def step_index(name: str) -> int:
    for i, step in enumerate(steps()):
        if step.get("name") == name:
            return i
    raise AssertionError(f"lane-plan.yml has no step named {name!r}")


def run_bash(body: str, env: dict) -> tuple[int, str, dict, str]:
    """Execute a step body with a real GITHUB_OUTPUT, return its outputs.

    Mirrors `run_bash` in test_lane_decisions.py: a fresh temp file for the
    output channel, a scrubbed environment so a host variable cannot satisfy a
    check the runner would not, and the stdout+stderr pair returned for
    assertions on messages.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out_file = pathlib.Path(tmp) / "out"
        out_file.touch()
        summary = pathlib.Path(tmp) / "summary"
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", body],
            env={
                "PATH": os.environ.get("PATH", ""),
                "GITHUB_OUTPUT": str(out_file),
                "GITHUB_STEP_SUMMARY": str(summary),
                **env,
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        outputs = {}
        for line in out_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
        summary_text = (
            summary.read_text(encoding="utf-8") if summary.exists() else ""
        )
        return proc.returncode, proc.stdout + proc.stderr, outputs, summary_text


class DetectCredentials(unittest.TestCase):
    """The presence probe is the whole switch; run its shell, read its output.

    The step reads `$HAS_APP_ID` / `$HAS_APP_PRIVATE_KEY` rather than
    `${{ secrets.* }}` inline, because a `secrets` context reference cannot be
    tested and because comparing a secret inline would put its value in the
    expression. So the runnable contract is the env-driven shell, and that is
    what these cases execute.
    """

    def probe(
        self, has_app_id: str, has_private_key: str
    ) -> tuple[int, str, dict, str]:
        body = step_by_name("Detect credentials")["run"]
        return run_bash(
            body,
            {"HAS_APP_ID": has_app_id, "HAS_APP_PRIVATE_KEY": has_private_key},
        )

    def test_both_secrets_present_reports_credentials(self):
        """The ordinary path: an org caller with both secrets is unaffected."""
        rc, log, outputs, _ = self.probe("true", "true")
        self.assertEqual(rc, 0, log)
        self.assertEqual(outputs.get("has_credentials"), "true")

    def test_absent_secrets_report_no_credentials(self):
        """THE case: an empty-credentials caller must not be told it failed.

        `github.actor != 'dependabot[bot]'` does NOT clear this for a fork, a
        public-only consumer, or any same-repo caller whose wrapper forwards no
        App secrets. The probe is what keeps those runs alive, so it has to
        answer `false` rather than error or stay silent.
        """
        for app_id, key in (("false", "false"), ("false", "true"),
                            ("true", "false")):
            with self.subTest(app_id=app_id, private_key=key):
                rc, log, outputs, _ = self.probe(app_id, key)
                self.assertEqual(rc, 0, log)
                self.assertEqual(
                    outputs.get("has_credentials"),
                    "false",
                    f"APP_ID={app_id} APP_PRIVATE_KEY={key} must read as "
                    "credential-free; a half-configured caller is exactly the "
                    "shape that fails the token mint",
                )

    def test_credential_free_mode_is_announced_not_silent(self):
        """A lane that changed behaviour says so in the run log and summary.

        Without this the credential-free path is indistinguishable from a
        normal one in the run's own output, which is what makes the class of
        failure here hard to diagnose from CI alone.
        """
        _, log, _, summary = self.probe("false", "false")
        self.assertIn("::notice::", log)
        self.assertIn("credential-free", log)
        self.assertIn("credential-free", summary)

    def test_a_present_credential_is_not_announced_as_absent(self):
        """The notice is the absence path's; it must not fire when present."""
        _, log, _, summary = self.probe("true", "true")
        self.assertNotIn("::notice::", log)
        self.assertNotIn("credential-free", summary)


class CredentialFreeFallback(unittest.TestCase):
    """The fallback arm is what keeps the chain alive; execute it.

    Without this step a credential-free caller produces no `runs_on`, no
    `provider` and no packaging answer, so every sibling lane that reads a
    plan output gets an empty string and the aggregate has no scope to
    adjudicate. The lane "succeeds" and the chain is still dead.
    """

    def fallback(self):
        body = step_by_name("Use native lane for credential-free caller")["run"]
        return run_bash(body, {})

    def test_the_fallback_fills_every_output_the_merge_gate_reads(self):
        rc, log, outputs, _ = self.fallback()
        self.assertEqual(rc, 0, log)
        for key in FALLBACK_STEP_OUTPUTS:
            with self.subTest(output=key):
                self.assertIn(key, outputs,
                              f"credential-free mode leaves `{key}` empty; an "
                              "empty plan output is what the merge gate reads "
                              "as 'no verdict'")
                self.assertNotEqual(
                    outputs[key], "",
                    f"`{key}` is empty in credential-free mode",
                )

    def test_every_step_output_reaches_a_job_output(self):
        """The two vocabularies must line up, or a filled step is a dropped output.

        The fallback writes the step-output name `runs-on` (hyphen, matching the
        resolver's own output) while the job declares the output `runs_on`
        (underscore, matching `workflow_call.outputs`). The job's `||` chain
        reads `steps.credentials-fallback.outputs.runs-on`, so the step side is
        the hyphen. If the two ever drifted apart the step would fill a key the
        job never reads, and `runs_on` would resolve empty in credential-free
        mode -- a chain that runs and cannot route itself.
        """
        outputs = load_lane_plan()["jobs"][JOB]["outputs"]
        declared = {key: str(expr) for key, expr in outputs.items()}
        self.assertEqual(
            sorted(declared), sorted(JOB_OUTPUTS),
            "the set of job outputs changed; update JOB_OUTPUTS so this suite "
            "still covers every output the merge gate reads",
        )
        for step_key in FALLBACK_STEP_OUTPUTS:
            with self.subTest(step_output=step_key):
                readers = [
                    key for key, expr in declared.items()
                    if f"steps.credentials-fallback.outputs.{step_key}" in expr
                ]
                self.assertTrue(
                    readers,
                    f"no job output reads `steps.credentials-fallback.outputs."
                    f"{step_key}`; the fallback sets it and nothing consumes it",
                )

    def test_the_fallback_routes_to_the_native_runner(self):
        """It must answer with a usable runner label set, not a placeholder.

        The labels are the same ones the Dependabot fallback uses: they are not
        invented here, they are the native self-hosted target the routing
        policy declares for this workflow's default lane.
        """
        _, _, outputs, _ = self.fallback()
        self.assertEqual(outputs["runs-on"], '["self-hosted","Linux","X64"]')
        self.assertEqual(outputs["provider"], "self-hosted")

    def test_the_fallback_scope_is_one_of_the_two_the_aggregate_accepts(self):
        """`scope` is load-bearing for the merge gate's skip waiver.

        The aggregate on a queue head accepts `merge-gate-only` and refuses
        anything unfamiliar, so a typo here is a red gate over a green chain
        rather than an obvious failure.
        """
        _, _, outputs, _ = self.fallback()
        self.assertIn(outputs["scope"], ("merge-gate-only", "full"))
        self.assertTrue(outputs["scope_reason"].strip(),
                        "a scope with no reason is unauditable")

    def test_the_fallback_does_not_claim_to_have_planned_packaging(self):
        """`packaging_impact=false` is the honest answer, not an optimistic one.

        The packaging policy counts Cargo.lock and Cargo.toml, so a Cargo bump
        really would have impact -- but the policy file is unreadable without
        the token. Reporting `true` here would be a fabricated verdict; the
        reason string has to say why it is answering false.
        """
        _, _, outputs, _ = self.fallback()
        self.assertEqual(outputs["packaging_impact"], "false")
        self.assertIn("credential-free", outputs["packaging_reason"])


class CredentialGateShape(unittest.TestCase):
    """The `if:` and `secrets:` properties, which no amount of running covers.

    A gate is an expression, not a script: there is nothing to execute, so
    these read the parsed workflow. They are still worth pinning, because the
    regression they catch is silent -- a step that loses its gate fails at run
    time with an error about an empty `app-id`, on a lane whose failure skips
    an entire repository's CI.
    """

    def test_the_app_secrets_are_optional_to_the_caller(self):
        """THE defect codex raised: `required: true` refuses the empty caller.

        A `workflow_call` secret marked required makes the CALLER's dispatch
        invalid, so "no credentials" cannot even be expressed, let alone
        tolerated.
        """
        secrets = workflow_call()["secrets"]
        for name in APP_SECRETS:
            with self.subTest(secret=name):
                self.assertIn(
                    name, secrets,
                    f"lane-plan.yml no longer declares `{name}`; a caller "
                    "cannot forward a secret the callee does not declare, so "
                    "the credential-free mode is unreachable",
                )
                self.assertIs(
                    secrets[name].get("required"),
                    False,
                    f"`{name}` is {secrets[name].get('required')!r}, not "
                    "`required: false`. A required secret makes an "
                    "empty-credentials caller an INVALID dispatch -- the "
                    "exact shape maxi-config#1028 set out to support.",
                )

    def test_every_credential_dependent_step_carries_the_gate(self):
        for name in CREDENTIAL_DEPENDENT_STEPS:
            with self.subTest(step=name):
                if_ = step_by_name(name).get("if", "")
                self.assertIn(
                    "steps.credentials.outputs.has_credentials == 'true'",
                    str(if_),
                    f"`{name}` cannot run without an App token and must be "
                    f"gated on it; its `if:` is {if_!r}. Un-gated, the token "
                    "mint dies with `client-id must be a non-empty string` and "
                    "every lane behind the plan is skipped.",
                )

    def test_the_change_detector_steps_stay_ungated(self):
        """The over-gating trap, pinned in the direction that preserves the fix.

        These two authenticate with the caller's own `github.token`, so they
        work with no App credentials. Gating them for symmetry with the steps
        above does not fail -- it silently degrades every docs-only change from
        a credential-free caller to `scope=full`, running the full chain on the
        PRs the classifier exists to keep cheap. See the module docstring.
        """
        for name in CREDENTIAL_INDEPENDENT_STEPS:
            with self.subTest(step=name):
                if_ = str(step_by_name(name).get("if", ""))
                self.assertNotIn(
                    "has_credentials", if_,
                    f"`{name}` runs on the caller's own github.token and must "
                    f"stay ungated; its `if:` is {if_!r}. Gating it makes the "
                    "real change-detector scope unreachable in credential-free "
                    "mode and silently forces scope=full on every docs-only "
                    "change from such a caller.",
                )

    def test_the_probe_precedes_every_step_that_reads_it(self):
        """An `if:` referencing a not-yet-set step output evaluates to `''`.

        GitHub does not fail that condition -- it reads it as empty and
        SKIPS the step. So moving `Detect credentials` below its first
        consumer does not error in review or in CI; it skips the token mint,
        the policy checkout and the resolver, every output resolves empty, and
        the whole chain quietly stops. Commit 124c4fe's subject is literally
        "order token detection" for this reason.
        """
        probe_at = step_index("Detect credentials")
        for name in CREDENTIAL_DEPENDENT_STEPS:
            with self.subTest(step=name):
                self.assertLess(
                    probe_at, step_index(name),
                    f"`Detect credentials` runs at step {probe_at} but "
                    f"`{name}` at step {step_index(name)} reads its output. A "
                    "forward reference reads as empty, which SKIPS the step "
                    "rather than failing it.",
                )

    def test_the_fallback_cannot_preempt_the_real_resolver(self):
        """The two fallbacks are mutually exclusive on `github.actor`.

        The Dependabot fallback is keyed on `github.actor == 'dependabot[bot]'`
        and the credential-free one on `has_credentials == 'false'`. A Dependabot
        run also has no App credentials, so if the credential-free arm were not
        actor-excluded both would fire and the later step's `$GITHUB_OUTPUT`
        append would win the `||` chain -- routing a Dependabot PR by the
        credential-free reason string.
        """
        dep = str(step_by_name("Use native lane for Dependabot").get("if", ""))
        self.assertIn("github.actor == 'dependabot[bot]'", dep)
        cred_free = str(
            step_by_name("Use native lane for credential-free caller").get("if", "")
        )
        self.assertIn("github.actor != 'dependabot[bot]'", cred_free)

    def test_the_job_outputs_all_carry_the_fallback_arm(self):
        """Each output's `||` chain must end at the credential-free fallback.

        Without the third arm a credential-free caller resolves an empty
        string for that output. The aggregate's skip waiver and the sibling
        lanes' `should_run` both read these, so an empty one is a chain that
        runs and decides nothing.
        """
        outputs = load_lane_plan()["jobs"][JOB]["outputs"]
        for key in JOB_OUTPUTS:
            with self.subTest(output=key):
                self.assertIn(
                    "steps.credentials-fallback.outputs",
                    outputs[key],
                    f"job output `{key}` has no credential-free fallback arm: "
                    f"{outputs[key]!r}. A credential-free caller resolves an "
                    "empty string here.",
                )


class CallerForwardsTheSecrets(unittest.TestCase):
    """The caller half: a secret the callee declares but nobody passes.

    `secrets: required: false` on the callee is only half the contract. The
    caller decides what actually arrives, and `rust-ci.yml`'s `plan:` job is
    what forwards them. A wrapper that stopped forwarding them would put every
    consumer into credential-free mode silently -- slower, advisory-only
    packaging -- with nothing failing.
    """

    def test_the_plan_caller_forwards_both_app_secrets(self):
        doc = yaml.safe_load(RUST_CI.read_text(encoding="utf-8"))
        forwarded = doc["jobs"]["plan"].get("secrets", {})
        for name in APP_SECRETS:
            with self.subTest(secret=name):
                self.assertIn(
                    name, forwarded,
                    f"rust-ci.yml `plan:` no longer forwards `{name}`; the "
                    "lane-plan credential-free fallback then takes over for "
                    "every consumer, quietly downgrading packaging to advisory",
                )
                self.assertIn(
                    "secrets.", str(forwarded[name]),
                    f"`plan:` forwards `{name}` as a literal {forwarded[name]!r} "
                    "rather than from the caller's secrets context",
                )


class NonVacuity(unittest.TestCase):
    """Guards on the guards.

    A contract test that stops finding its steps still passes unless something
    asserts that it found them. `step_by_name` raises on a miss, but a suite
    whose class body silently emptied itself would report green while testing
    nothing -- which is the exact failure mode this repository has hit before
    (maxi-config#763: a ruleset pinning a context no workflow emits, green for
    75 minutes across 31 repos).
    """

    def test_the_test_actually_finds_the_steps_it_pins(self):
        found = {step.get("name") for step in steps()}
        for name in (
            "Detect credentials",
            *CREDENTIAL_DEPENDENT_STEPS,
            *CREDENTIAL_INDEPENDENT_STEPS,
            "Use native lane for credential-free caller",
            "Use native lane for Dependabot",
        ):
            with self.subTest(step=name):
                self.assertIn(name, found)

    def test_the_probe_is_a_step_with_an_id(self):
        """Every consumer spells it `steps.credentials.outputs...`.

        Renaming the `id:` without updating the four `if:`s would leave each
        gate reading an empty output -- which skips, not fails.
        """
        self.assertEqual(
            step_by_name("Detect credentials").get("id"),
            "credentials",
            "the probe's `id:` changed; every gate references it by name, and "
            "a stale reference reads empty, which SKIPS rather than fails",
        )


if __name__ == "__main__":
    unittest.main()
