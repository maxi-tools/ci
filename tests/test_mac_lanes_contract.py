#!/usr/bin/env python3
"""Pin the mac_lanes contract between rust-ci.yml and its consumers.

WHAT THE CONTRACT IS. A wrapper repository keeps its heavy macOS jobs in
standalone workflows (maximoji-rs apple.yml, maxi-ml-mac-app
waterui-app-bundle.yml, ...) that trigger on `pull_request` directly. Those
jobs claim the scarce Mac runners even when the template's cheap Linux lanes
are about to fail. The fix has two halves, and this file pins both:

  1. `rust-ci.yml` declares the optional `mac_lanes` input (the consumer's
     declaration that it has standalone Mac lanes) and the `linux_gate`
     output (the merge-gate aggregate's RESULT, published for exactly those
     workflows to read). The input is inert -- nothing in rust-ci.yml reads
     it -- and that is deliberate: GitHub does not resolve expressions in
     `jobs.<id>.uses`, so the template cannot call a consumer-local workflow
     by a path taken from an input, and a relative `./` path resolves in the
     CALLEE's checkout (maxi-tools/ci), never the consumer's. The input
     exists so the wrapper fan-out (scripts/rust_ci_render.py in
     maxi-config) can carry the consumer's declaration -- the renderer
     special-cases a NON-EMPTY consumer mac_lanes the way it special-cases
     a fork's repo_policy, because the template declares the key and the
     ordinary template-wins rule would reset the declaration to '' on
     every tick -- and so `grep mac_lanes .github/workflows/ci.yml`
     is a true statement about which repos adopted the chain.

  2. The consumer's standalone workflow reads the gate through the
     `workflow_run` completion trigger (`workflow_run.artifacts` can carry
     nothing it needs; it needs only `github.event.workflow_run.conclusion`),
     which holds NO Linux runner while waiting -- the reason a polling or
     `needs:`-style wait was rejected (Linux is the bottleneck). Passing
     values are `success` and `skipped`, the same allowlist the merge-gate's
     own Aggregate step applies: fan-out and docs-only PRs skip lanes
     deliberately, and a Mac lane must not wedge forever on a PR whose lanes
     were correctly skipped.

WHY THE TEST IS HERE AND NOT IN MAXI-CONFIG. This repository is the public
mirror; the copy of rust-ci.yml that the fleet actually executes at a pinned
SHA is THIS one (maxi-config's test_public_ci_copy_agrees.py fails the whole
org if the two copies drift). A contract test that only ran in maxi-config
would still pass while the fleet ran a copy missing the output.

The mirror-side wiring test is test_public_ci_copy_agrees.py in maxi-config:
it byte-compares the spliced region on both sides, so an edit to one copy
without the other is a red build there before it is a red run here.

Run directly: `python3 tests/test_mac_lanes_contract.py`.
Run in CI: `self-check.yml`'s "Run the test suites" step.
"""

from __future__ import annotations

import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUST_CI = ROOT / ".github/workflows/rust-ci.yml"

# `skipped` means "the lanes were deliberately not run" (fan-out PR,
# docs-only diff, Dependabot). That is not a verdict against the tree, so
# the consumer's Mac lane proceeds. MUST equal the PASSING tuple inside
# merge-gate's "Aggregate lane results" step -- asserted in the test body
# by reading that step's source, so the two cannot drift apart in silence.
PASSING_RESULTS = ("success", "skipped")


def load_workflow(path: pathlib.Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "jobs" not in doc:
        raise ValueError(f"{path.name}: expected a workflow-shaped YAML mapping")
    return doc


def workflow_call(doc: dict) -> dict:
    # PyYAML parses an unquoted `on` key as boolean True (YAML 1.1).
    triggers = doc["on"] if "on" in doc else doc[True]
    return triggers["workflow_call"]


class MacLanesContractTest(unittest.TestCase):
    def test_mac_lanes_input_is_declared_and_optional(self):
        wc = workflow_call(load_workflow(RUST_CI))
        mac_lanes = wc.get("inputs", {}).get("mac_lanes")
        self.assertIsNotNone(
            mac_lanes,
            "rust-ci.yml must declare the mac_lanes input; a consumer "
            "setting it on the wrapper's merge-gate job otherwise fails "
            "run creation with 'unexpected input'",
        )
        self.assertFalse(
            mac_lanes.get("required", False),
            "mac_lanes must be optional: the 57 wrappers that have no "
            "standalone Mac workflow must keep working unchanged, which is "
            "the whole point of making this an opt-in rather than a "
            "breaking template change",
        )
        self.assertEqual(
            mac_lanes.get("type"),
            "string",
            "mac_lanes must keep its declared `type: string`: another type "
            "would still satisfy optionality here yet break callers that "
            "supply the lane declaration as a string",
        )

    def test_linux_gate_output_publishes_the_aggregate_result(self):
        wc = workflow_call(load_workflow(RUST_CI))
        gate = wc.get("outputs", {}).get("linux_gate")
        self.assertIsNotNone(
            gate,
            "rust-ci.yml must publish the linux_gate output; without it a "
            "standalone Mac workflow has nothing to read and the fail-fast "
            "chain does not exist",
        )
        self.assertIn(
            "jobs.merge-gate.outputs.gate_result",
            gate.get("value", ""),
            "linux_gate must publish the merge-gate aggregate's conclusion. "
            "That job is the SOLE required context and waits on every "
            "declared lane with always() in its if:, so its result is "
            "exactly 'the Linux gate concluded, and here is which way' -- "
            "including the failure and cancelled conclusions a boolean "
            "would flatten. The value is routed through a job-level output "
            "(steps.gate-result, `job.status` in an always() step) rather "
            "than a direct `jobs.merge-gate.result` read, because "
            "actionlint's reusable-call typing flags the direct form "
            "(rhysd/actionlint#343) and self-check fails on actionlint",
        )

    def test_merge_gate_always_runs_so_the_output_always_publishes(self):
        jobs = load_workflow(RUST_CI)["jobs"]
        if_ = jobs["merge-gate"].get("if", "")
        self.assertIn(
            "always()",
            if_,
            "merge-gate's if: must keep always(); if a failed lane could "
            "skip the aggregate, linux_gate would never publish on exactly "
            "the PRs the fail-fast chain exists for, and the consumer's Mac "
            "lane would wedge on a PR whose CI just went red",
        )

    def test_passing_results_match_the_aggregate_allowlist(self):
        """`skipped`-is-fine must be ONE rule, not two.

        The Aggregate step inside merge-gate defines which lane results pass
        the gate. The consumer-side Mac-lane gate (this contract's step 2)
        must apply the same allowlist, or the two halves of the chain
        disagree -- a PR whose lanes were legitimately skipped would pass
        merge-gate and then be refused by the Mac workflow, with nothing
        naming the mismatch.
        """
        text = RUST_CI.read_text(encoding="utf-8")
        self.assertIn(
            'PASSING = ("success", "skipped")',
            text,
            "merge-gate's Aggregate step must keep its PASSING allowlist in "
            "the literal form this test greps; the consumer-side Mac-lane "
            "gate copies that rule and this assertion is the coupling",
        )

    def test_no_consumer_workflow_is_referenced_from_the_template(self):
        """The template must not pretend to call a consumer's workflow.

        GitHub resolves `uses:` statically: no expressions, and a relative
        `./` path that resolves against THIS repository's checkout, not the
        consumer's. An attempt to honour mac_lanes by calling a workflow
        named in it would either fail run creation or silently call a file
        in maxi-tools/ci -- the wrong repo -- on every consumer at once.
        The declaration is consumed by the consumer's own workflows, which
        is the only place GitHub's trigger model can express the chain.

        ALLOWED EXCEPTION (the merge-gate-incorporated lane): a hard-coded
        `lane-mac-waterui` job whose `if:` reads `${{ inputs.mac_lanes }}`
        is the documented shape for funneling a consumer's mac-waterui lane
        INTO the merge-gate aggregate without violating the dynamic-`uses:`
        dead end. The literal string `mac_lanes` is permitted inside that
        job's `if:` (the gate) and inside the merge-gate aggregate step's
        `env:` binding (so skip_reason can read the same signal). Anywhere
        ELSE in the jobs section -- in another job's `uses:` value, in a
        step's `with:`, in a `uses:` argument on `lane-mac-waterui` itself,
        on the `mac_lanes` string without the `${{ inputs.X }}` shape --
        is rejected. The pin is intentionally narrow: a future commit that
        reaches for `mac_lanes` to dispatch a different lane is exactly the
        dynamic-`uses:`-style mistake this test exists to catch.
        """
        import re

        doc = yaml.safe_load(RUST_CI.read_text(encoding="utf-8"))
        jobs = doc.get("jobs", {}) or {}

        # Every concrete value a `uses:` would resolve to. GitHub does NOT
        # accept expressions in `uses:` -- the value is literal -- so a
        # string interpolation here would fail run creation or, worse, a
        # relative path would silently resolve inside this repo on every
        # consumer at once.
        for job_id, job in jobs.items():
            uses = job.get("uses")
            if isinstance(uses, str) and "mac_lanes" in uses:
                self.fail(
                    f"job {job_id!r}: `uses: {uses}` interpolates `mac_lanes` "
                    "into a value GitHub resolves statically. A "
                    "`uses: ./<workflow>.yml@${{ inputs.mac_lanes }}`-style "
                    "ship broke run creation; this test pins the failure."
                )

            # `with:` values: mac_lanes as a value (even if inside an
            # expression) would also reach into the consumer's workflows.
            for step in job.get("steps", []) or []:
                with_block = step.get("with") or {}
                for key, val in with_block.items():
                    if isinstance(val, str) and "mac_lanes" in val:
                        self.fail(
                            f"job {job_id!r}, step {step.get('name')!r}: "
                            f"`with: {key}: {val}` interpolates `mac_lanes` "
                            "into a value. mac_lanes is a wrapper-side "
                            "declaration; the reusable it calls owns its "
                            "own inputs."
                        )

        # Now a narrower pass: `mac_lanes` may appear in jobs ONLY inside
        # one of the documented gate expressions -- on the
        # `lane-mac-waterui` job's `if:`, or on the `merge-gate` job's
        # aggregate step's `env:` binding (so the skip_reason predicate
        # can read the same signal). Any other surface (a free token in
        # another job, a stray env binding, a comment-as-instruction in
        # another job's body) is rejected by the broad `uses:` /
        # `with:` check above; the gate-expression-only pass below keeps
        # the permitted occurrences to exactly the two named sites.
        gate_pat = re.compile(r"\${{\s*inputs\.mac_lanes[^}]*}}")
        permitted_gate_sites: list[str] = []

        # Lane-mac-waterui: if: must read inputs.mac_lanes.
        lwm = jobs.get("lane-mac-waterui") or {}
        lwm_if = lwm.get("if", "")
        if gate_pat.search(lwm_if):
            permitted_gate_sites.append("lane-mac-waterui.if")
        else:
            self.fail(
                "the `lane-mac-waterui` job's `if:` must gate on "
                "`${{ inputs.mac_lanes ... }}`; without it the gate runs "
                "on every consumer (including the 56 wrappers that did "
                "not adopt the lane)."
            )

        # Merge-gate aggregate step env: must bind inputs.mac_lanes into
        # MAC_LANES_VALUE (or equivalent), so the Python skip_reason
        # can read it.
        mg = jobs.get("merge-gate") or {}
        for step in mg.get("steps", []) or []:
            if step.get("name") != "Aggregate lane results":
                continue
            env_block = step.get("env") or {}
            for env_val in env_block.values():
                if isinstance(env_val, str) and gate_pat.search(env_val):
                    permitted_gate_sites.append(
                        "merge-gate.steps[Aggregate lane results].env"
                    )
                    break
        self.assertIn(
            "merge-gate.steps[Aggregate lane results].env",
            permitted_gate_sites,
            "the merge-gate aggregate step's `env:` must bind "
            "`${{ inputs.mac_lanes }}` (e.g. MAC_LANES_VALUE) so the "
            "skip_reason predicate reads the same gate signal the lane's "
            "`if:` applied. Without this binding the aggregate has no "
            "way to distinguish an explained skip (non-adopter) from an "
            "unexplained one (red lane).",
        )

        # No other job may name `mac_lanes` in its `if:`, `name:`,
        # `description:`, or anywhere else. The narrow surfaces a
        # future refactor would reach for -- a free token in another
        # job, a stray env binding, a `with:` value -- are caught by
        # the `uses:` / `with:` passes above. This assertion closes
        # the `if:` half of that surface (gate_pat already excludes
        # those sites from the global check).
        for job_id, job in jobs.items():
            if job_id in ("lane-mac-waterui", "merge-gate"):
                continue  # both permitted gate sites above
            job_body = str(job)
            if gate_pat.search(job_body):
                self.fail(
                    f"job {job_id!r}: the `${{{{ inputs.mac_lanes ... }}}}` "
                    "expression is permitted ONLY on `lane-mac-waterui`'s "
                    "`if:` (and on `merge-gate`'s aggregate step's `env:`, "
                    "as a path for `skip_reason` to read). Anywhere else "
                    "(in another job's `if:`, on the inputs that flow into "
                    "a `uses:`) is the dynamic-`uses:` mistake this test "
                    "exists to catch."
                )

    def test_lane_mac_waterui_is_in_the_merge_gate_needs_chain(self):
        """Closing the loop requires the new lane in merge-gate's needs.

        Without `lane-mac-waterui` in the merge-gate aggregate's `needs:`,
        the new job runs but its result never reaches the gate's PASSING
        allowlist check, and the consumer-side `Mac WaterUI test` lane
        stays as advisory as it was before this card. The LANES tuple in
        the aggregate step must name it too, so a missing-from-needs lane
        fails the `bad += [(name, '<missing>') ...]` path with a clear
        error rather than silently passing.
        """
        text = RUST_CI.read_text(encoding="utf-8")
        # The merge-gate job's needs: list. Most specific match: the line
        # under `merge-gate:` that starts with `needs:`.
        import re

        m: "re.Match[str] | None" = re.search(
            r"^ {2}merge-gate:\n(?: {4}[^\n]*\n)+",
            text, flags=re.MULTILINE,
        )
        self.assertIsNotNone(
            m,
            "rust-ci.yml must declare a `merge-gate:` job; the rest of the "
            "contract assumes one",
        )
        needs_block = m.group(0)
        self.assertIn(
            "lane-mac-waterui", needs_block,
            "merge-gate's `needs:` must include `lane-mac-waterui`: the "
            "card's whole purpose is making the mac-waterui verdict roll "
            "up under the merge-gate aggregate, and the aggregate only "
            "reads the jobs in `needs:`. Without this entry the new lane "
            "publishes its own check run but the ruleset never sees it.",
        )

    def test_lane_mac_waterui_aggregate_skip_reason_refs_consumer_opt_in(self):
        """The aggregate must classify the lane as a consumer opt-in skip.

        The `lane-mac-waterui` job is heavy (Mac-primary), so the
        merge-gate's `skip_reason` predicate must reach it on the
        `HEAVY_LANES` branch AND recognise the consumer opt-in shape (an
        empty `mac_lanes` declaration). Without this test, a future
        refactor that drops `lane-mac-waterui` from `HEAVY_LANES` would
        silently turn it into a `RELEASE_LANES`-shape or fall through the
        `def skip_reason(): return None` default -- both of which would
        reject the legitimate skip a non-adopter sees today (maximoji-rs,
        maxi-ml-mac-app, ... -- the 56 wrappers that the lane is opt-in
        for), and 56 PRs a week would red.
        """
        text = RUST_CI.read_text(encoding="utf-8")
        # Both halves are required: the lane must be in HEAVY_LANES (else
        # skip_reason returns None for it), AND the consumer-opt-in branch
        # must explain an empty `mac_lanes`.
        self.assertIn(
            '("check", "test", "lane-mac-waterui")', text,
            "the aggregate's HEAVY_LANES tuple must include "
            "`lane-mac-waterui` so `skip_reason` reaches the heavy-lane "
            "branch for it. Without that entry, an empty mac_lanes "
            "declaration is treated as an unexplained skip and the gate "
            "fails on the 56 wrappers that did not opt in.",
        )
        self.assertIn(
            "consumer opt-in: mac_lanes declared empty", text,
            "the aggregate's skip_reason must explain a skipped "
            "lane-mac-waterui as the consumer opt-in shape; an unexplained "
            "skip on a heavy lane is a gate fail.",
        )


if __name__ == "__main__":
    unittest.main()
