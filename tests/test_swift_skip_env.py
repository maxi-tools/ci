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

    def test_rust_ci_forwards_skip_swift_build_to_check_test_and_package(self):
        """skip_swift_build is forwarded to lane-check, lane-test, and lane-package.

        lane-check and lane-test skip the Swift build for maximoji-apple
        (maxi-tray#460) on Rust-only PRs. lane-package also accepts the
        forwarded input -- its workflow_call.inputs declares it (see
        `.github/workflows/lane-package.yml`) and forwards it onto
        MAXIMOJI_APPLE_SKIP_SWIFT_BUILD inside the job -- so a packaging
        lane on a docs-only PR keeps the same skip behaviour as
        check/test. (coderabbit P2 review thread: the previous version
        of this test asserted `skip_swift_build:` was absent from
        `package`, which only happened because `package`'s call site
        had been wrongfully stripped of the forwarding under a
        false-premise comment claiming the input was undeclared.
        Restoring the forwarding matches the declared lane contract.)

        Asserts by parsing each lane's `with:` block: the input
        expression is bound to the expected upstream `with:` token so a
        comment or unrelated `with:` entry containing the bare word
        cannot make the test pass (the regression class the path
        instructions flag).
        """
        text = (ROOT / ".github/workflows/rust-ci.yml").read_text(encoding="utf-8")

        def with_block(job_name: str) -> str:
            # Match the top-level `  <job>:` header and read until the
            # next top-level job header, or the permissions/secrets
            # block that follows the `with:` mapping.
            match = re.search(
                r"^ {2}" + re.escape(job_name) + r":\n(?P<body>(?:(?!^ {2}[A-Za-z0-9_-]+:).)*)",
                text,
                re.MULTILINE | re.DOTALL,
            )
            if match is None:
                self.fail("rust-ci.yml has no job named " + job_name)
            body = match.group(0)
            with_match = re.search(r"\bwith:\n(?P<with>(?: {6,}[^\n]*\n)+)", body)
            if with_match is None:
                self.fail(f"rust-ci.yml job {job_name} has no `with:` mapping")
            return with_match.group("with")

        expected = "skip_swift_build: ${{ inputs.skip_swift_build == true }}"
        for job_name in ("check", "test", "package"):
            with self.subTest(job=job_name):
                self.assertIn(
                    expected,
                    with_block(job_name),
                    f"{job_name} must forward `skip_swift_build: ${{{{ "
                    f"inputs.skip_swift_build == true }}}}` to its lane; the "
                    "exact-expression check guards against a comment or "
                    "unrelated `with:` entry that mentions the bare word.",
                )


if __name__ == "__main__":
    unittest.main()
