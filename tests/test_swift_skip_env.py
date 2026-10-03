#!/usr/bin/env python3
"""skip_swift_build maps to MAXIMOJI_APPLE_SKIP_SWIFT_BUILD identically.

maxi-tray#460: maximoji-apple treats an empty string as a hard error, not
"don't skip". The GHA `cond && '1' || ''` OFF spelling is therefore
forbidden. True must write exactly `1`; false must leave the variable
unset. lane-check, lane-test, and lane-package must share that mapping.

Run directly: `python3 tests/test_swift_skip_env.py`.
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
ENV_NAME = "MAXIMOJI_APPLE_SKIP_SWIFT_BUILD"
STEP_NAME = "Map skip_swift_build onto MAXIMOJI_APPLE_SKIP_SWIFT_BUILD"
EMPTY_SPELLING = re.compile(
    r"MAXIMOJI_APPLE_SKIP_SWIFT_BUILD:\s*\$\{\{\s*"
    r"inputs\.skip_swift_build\s*&&\s*'1'\s*\|\|\s*''"
)


def load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def workflow_inputs(doc: dict) -> dict:
    # PyYAML still treats YAML 1.1 `on`/`off` as booleans, so `on:` loads as True.
    trigger = doc.get("on")
    if trigger is None:
        trigger = doc[True]
    return trigger["workflow_call"]["inputs"]


def named_step(doc: dict, name: str) -> dict:
    matches = []
    for job in doc["jobs"].values():
        for step in job.get("steps") or []:
            if step.get("name") == name:
                matches.append(step)
    if len(matches) != 1:
        raise AssertionError(f"expected 1 step named {name!r}, got {len(matches)}")
    return matches[0]


class SwiftSkipEnvMapping(unittest.TestCase):
    def test_all_three_lanes_map_skip_swift_build_identically(self):
        mappings = []
        for path in LANES:
            doc = load(path)
            inputs = workflow_inputs(doc)
            self.assertIn("skip_swift_build", inputs, path.name)
            self.assertEqual(inputs["skip_swift_build"].get("type"), "boolean")
            env = doc.get("env") or {}
            self.assertNotIn(
                ENV_NAME,
                env,
                f"{path.name} must not set {ENV_NAME} in workflow env; "
                "that emits an empty string when skip_swift_build is false",
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotRegex(text, EMPTY_SPELLING)
            step = named_step(doc, STEP_NAME)
            mappings.append(
                (path.name, step.get("if"), step.get("run"), step.get("shell"))
            )
        first_name, *first = mappings[0]
        for name, *rest in mappings[1:]:
            self.assertEqual(
                tuple(rest),
                tuple(first),
                f"{name} mapping differs from {first_name}",
            )

    def test_mapping_writes_one_and_never_an_empty_value(self):
        for path in LANES:
            with self.subTest(lane=path.name):
                step = named_step(load(path), STEP_NAME)
                cond = step.get("if") or ""
                self.assertIn("inputs.skip_swift_build", cond)
                run = step["run"]
                self.assertIn(f"{ENV_NAME}=1", run)
                remainder = run.replace(f"{ENV_NAME}=1", "")
                self.assertNotIn(f"{ENV_NAME}=", remainder)
                self.assertNotIn("|| ''", run)
                self.assertNotIn('=""', run)
                self.assertNotIn("=''", run)

    def test_rust_ci_forwards_skip_swift_build_to_package(self):
        text = (ROOT / ".github/workflows/rust-ci.yml").read_text(encoding="utf-8")
        package = re.search(
            r"^ {2}package:\n(?P<body>(?:(?!^ {2}[A-Za-z0-9_-]+:).)*)",
            text,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(package)
        self.assertIn("skip_swift_build:", package.group(0))


if __name__ == "__main__":
    unittest.main()
