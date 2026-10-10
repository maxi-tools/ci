#!/usr/bin/env python3
"""The apt install step must run on a checked-out tree, in every lane.

ci#52 added repo-declared package installation to lane-package before the
actions/checkout step. The composite action reads ci/apt-packages.txt from
the consumer's checkout: before checkout the file cannot exist, the step's
`[ -f ]` guard exits 0, and a lane that needed the packages built without
them. lane-check and lane-test already installed after checkout; lane-package
did not -- a regression nothing saw because the step exits 0 either way.

The companion defect: the same PR handed actions/checkout a required `token`
input expression-conditioned on checkout_submodules, so a `false` (every
non-submodule caller) evaluated to an explicit empty string. The fix removed
the `token` key entirely -- the ambient GITHUB_TOKEN fallback satisfies the
required input, which is what lane-check and lane-test already relied on.
Both paths (submodule and non-submodule callers) are pinned here.

Run directly: `python3 tests/test_apt_install_ordering.py`.
"""

from __future__ import annotations

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LANES = (
    ROOT / ".github/workflows/lane-check.yml",
    ROOT / ".github/workflows/lane-test.yml",
    ROOT / ".github/workflows/lane-package.yml",
)
INSTALL_STEP = "Install repo-declared system packages"
CHECKOUT_USES = re.compile(r"^actions/checkout@")
EXPECTED_SUBMODULES = "inputs.checkout_submodules&&'recursive'||'false'"


def load(path: pathlib.Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def job_steps(doc):
    steps = []
    for job in doc["jobs"].values():
        steps.extend(job.get("steps") or [])
    return steps


def checkout_steps(steps):
    return [step for step in steps if CHECKOUT_USES.match(str(step.get("uses") or ""))]


class AptInstallOrdering(unittest.TestCase):
    def test_install_runs_after_checkout_in_every_lane(self):
        for path in LANES:
            with self.subTest(lane=path.name):
                steps = job_steps(load(path))
                checkouts = checkout_steps(steps)
                self.assertTrue(checkouts, f"{path.name}: no checkout step")
                installs = [
                    index
                    for index, step in enumerate(steps)
                    if step.get("name") == INSTALL_STEP
                ]
                self.assertEqual(
                    len(installs),
                    1,
                    f"{path.name}: expected exactly one {INSTALL_STEP!r} step",
                )
                last_checkout = max(
                    index for index, step in enumerate(steps) if step in checkouts
                )
                self.assertGreater(
                    installs[0],
                    last_checkout,
                    f"{path.name}: {INSTALL_STEP!r} must follow checkout; "
                    "before checkout ci/apt-packages.txt cannot exist and "
                    "the step no-ops with exit 0",
                )

    def test_checkout_never_passes_an_explicit_token(self):
        for path in LANES:
            with self.subTest(lane=path.name):
                for step in checkout_steps(job_steps(load(path))):
                    self.assertNotIn(
                        "token",
                        step.get("with") or {},
                        f"{path.name}: a `token` input expression-conditioned "
                        "on checkout_submodules hands an empty required string "
                        "to every non-submodule caller",
                    )

    def test_checkout_submodules_expression_covers_both_paths(self):
        for path in LANES:
            with self.subTest(lane=path.name):
                checkouts = checkout_steps(job_steps(load(path)))
                self.assertTrue(checkouts, f"{path.name}: no checkout step")
                step = checkouts[0]
                submodules = str((step.get("with") or {}).get("submodules") or "")
                # Normalise the ${{ ... }} wrapper and whitespace, then pin the
                # exact mapping: checkout_submodules=true must initialise
                # recursively and false must not. Merely mentioning the input
                # would also accept an expression that always disables.
                normalised = re.sub(r"\s+", "", submodules)
                normalised = normalised.removeprefix("${{").removesuffix("}}")
                self.assertEqual(
                    normalised,
                    EXPECTED_SUBMODULES,
                    f"{path.name}: checkout `submodules` must map the "
                    "checkout_submodules input so recursive initialisation "
                    "reaches submodule callers and stays off for the rest",
                )


if __name__ == "__main__":
    unittest.main()
