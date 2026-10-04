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
        self.assertEqual(
            mac_lanes.get("required", False), False,
            "mac_lanes must be optional: the 57 wrappers that have no "
            "standalone Mac workflow must keep working unchanged, which is "
            "the whole point of making this an opt-in rather than a "
            "breaking template change",
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
            "jobs.merge-gate.outputs.gate_result", gate.get("value", ""),
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
            "always()", if_,
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
            'PASSING = ("success", "skipped")', text,
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
        """
        text = RUST_CI.read_text(encoding="utf-8")
        self.assertNotIn(
            "mac_lanes", text.split("jobs:", 1)[1],
            "rust-ci.yml's jobs section must not reference mac_lanes. The "
            "input is a declaration for the consumer's own standalone "
            "workflows (and the wrapper fan-out preserves a non-empty "
            "value via rust_ci_render.py's mac_lanes special-case), not a "
            "value the template can act on: `uses:` takes no expressions, "
            "and a relative path would resolve inside maxi-tools/ci rather "
            "than the consumer. If GitHub ever ships dynamic `uses:`, "
            "revisit this test deliberately, not by deleting it",
        )


if __name__ == "__main__":
    unittest.main()
