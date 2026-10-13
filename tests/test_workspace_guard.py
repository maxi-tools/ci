#!/usr/bin/env python3
"""Cargo runs only when the checkout has a root Cargo.toml.

The decision is a shell step, invisible to actionlint. These tests extract
that step and RUN it, then check that every cargo (and toolchain) step is
behind the output it sets. A comment mentioning the gate must not satisfy
the second half: the assertion reads the parsed `if:`, not the raw file.

lane-check and lane-test grew the gate in ci#44. lane-package did not.
`should_run` is true on main and on release refs even when the change has
no packaging impact, so a non-Rust consumer push reached `cargo fetch` and
died with "could not find Cargo.toml" (maxi-nix main, 2026-09-26). The
skip is on absence, not on failure: a root manifest that is present still
sets the output true, and a fetch that then fails still reports red.

Run directly: `python3 tests/test_workspace_guard.py`.
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LANES = (
    ROOT / ".github/workflows/lane-check.yml",
    ROOT / ".github/workflows/lane-test.yml",
    ROOT / ".github/workflows/lane-package.yml",
)
WORKSPACE_GATE = "steps.workspace.outputs.has_workspace == 'true'"
NOTICE = "::notice::No Cargo.toml at repo root"
# A command word, not a mention in prose. Comments are skipped by the caller.
CARGO_CMD = re.compile(r"(^|[;&|`(]\s*)cargo\s+\S")
RUSTC_CMD = re.compile(r"(^|[;&|`(]\s*)rustc\s")


def step_run(path: pathlib.Path, step_name: str) -> str:
    """The `run:` body of one named step, or fail loudly."""
    step = named_step(path, step_name)
    if "run" not in step:
        raise AssertionError(f"{path.name} step {step_name!r} has no run: block")
    return step["run"]


def named_step(path: pathlib.Path, step_name: str) -> dict:
    matches = [s for s in all_steps(path) if s.get("name") == step_name]
    if len(matches) != 1:
        raise AssertionError(
            f"{path.name} has {len(matches)} steps named {step_name!r}; "
            "this test reads that step directly and a rename must not "
            "silently stop testing it"
        )
    return matches[0]


def all_steps(path: pathlib.Path) -> list:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = []
    for job in doc["jobs"].values():
        steps.extend(job.get("steps") or [])
    return steps


def run_detect(body: str, tree: pathlib.Path) -> tuple[int, str, dict, str]:
    """Execute the detect step with a real GITHUB_OUTPUT and summary file."""
    out_file = tree / "github_output"
    summary_file = tree / "github_summary"
    out_file.touch()
    summary_file.touch()
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", "-c", body],
        cwd=tree,
        env={
            "PATH": os.environ.get("PATH", ""),
            "GITHUB_OUTPUT": str(out_file),
            "GITHUB_STEP_SUMMARY": str(summary_file),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    outputs = {}
    for line in out_file.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            outputs[key] = value
    log = proc.stdout + proc.stderr
    return proc.returncode, log, outputs, summary_file.read_text(encoding="utf-8")


def command_lines(run: str) -> list[str]:
    lines = []
    for line in (run or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(stripped)
    return lines


def needs_rust_workspace(step: dict) -> bool:
    """True when the step cannot succeed on a tree with no root Cargo.toml."""
    run = step.get("run") or ""
    uses = step.get("uses") or ""
    commands = command_lines(run)
    if any(CARGO_CMD.search(line) for line in commands):
        return True
    if any(RUSTC_CMD.search(line) for line in commands):
        return True
    if "rust-toolchain" in uses or "rust-cache" in uses:
        return True
    if "select-rust-toolchain.sh" in run or "emit-runner-paths.sh" in run:
        return True
    # if-no-files-found: error. An upload of target/ with nothing built is a
    # red lane, which is the same failure moved one step later.
    if step.get("name") == "Upload release artifacts":
        return True
    # Writes the insteadOf token the fetch step consumes. Leaving it ungated
    # is not the cargo failure, but it is the step that exists only to make
    # that fetch authenticate.
    if step.get("name") == "Configure git for private deps":
        return True
    if step.get("name") == "Fetch maxi-config rust-lane policy":
        return True
    return False


class DetectRustWorkspace(unittest.TestCase):
    """Positive Rust and negative non-Rust, executed, for every cargo lane."""

    def exercise(self, path: pathlib.Path, tree: pathlib.Path):
        body = step_run(path, "Detect Rust workspace")
        return run_detect(body, tree)

    def test_a_root_manifest_is_a_rust_workspace(self):
        """Presence, not validity. A broken manifest still runs cargo and goes red."""
        for path in LANES:
            with self.subTest(lane=path.name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = pathlib.Path(tmp)
                    (root / "Cargo.toml").write_text(
                        "[package]\nname = \"probe\"\nversion = \"0.0.0\"\n",
                        encoding="utf-8",
                    )
                    rc, log, outputs, summary = self.exercise(path, root)
                self.assertEqual(rc, 0, log)
                self.assertEqual(outputs.get("has_workspace"), "true", log)
                self.assertNotIn(NOTICE, log)
                self.assertNotIn("absent", summary)

    def test_no_manifest_skips_rather_than_failing(self):
        """THE defect: absence must be a clean skip, not exit 101 from cargo."""
        for path in LANES:
            with self.subTest(lane=path.name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = pathlib.Path(tmp)
                    rc, log, outputs, summary = self.exercise(path, root)
                self.assertEqual(rc, 0, log)
                self.assertEqual(outputs.get("has_workspace"), "false", log)
                self.assertIn(NOTICE, log)
                self.assertIn("absent", summary)
                self.assertIn("Rust steps skipped", summary)

    def test_a_nested_manifest_is_not_a_workspace_cargo_recognises(self):
        """A member under crates/ with no root manifest fails cargo the same way."""
        for path in LANES:
            with self.subTest(lane=path.name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = pathlib.Path(tmp)
                    member = root / "crates" / "foo"
                    member.mkdir(parents=True)
                    (member / "Cargo.toml").write_text(
                        "[package]\nname = \"foo\"\nversion = \"0.0.0\"\n",
                        encoding="utf-8",
                    )
                    rc, log, outputs, summary = self.exercise(path, root)
                self.assertEqual(rc, 0, log)
                self.assertEqual(outputs.get("has_workspace"), "false", log)
                self.assertIn("absent", summary)


class CargoStepsRequireTheWorkspace(unittest.TestCase):
    def test_every_cargo_and_toolchain_step_is_gated_on_the_output(self):
        for path in LANES:
            ungated = []
            for step in all_steps(path):
                if not needs_rust_workspace(step):
                    continue
                cond = step.get("if") or ""
                if WORKSPACE_GATE not in cond:
                    ungated.append(step.get("name") or step.get("uses") or "<unnamed>")
            with self.subTest(lane=path.name):
                self.assertEqual(
                    ungated,
                    [],
                    f"{path.name} still reaches cargo without a root Cargo.toml: {ungated}",
                )

    def test_package_keeps_the_caller_deferral(self):
        """Adding the workspace gate must not drop should_run.

        A deferred package lane does not check the tree out. Detect runs
        only then, so a leftover Cargo.toml on a persistent runner must not
        be treated as this job's workspace either -- both halves of the if
        have to hold.
        """
        path = ROOT / ".github/workflows/lane-package.yml"
        detect = named_step(path, "Detect Rust workspace")
        self.assertIn("inputs.should_run", detect.get("if") or "")
        dropped = []
        for step in all_steps(path):
            if not needs_rust_workspace(step):
                continue
            if "inputs.should_run" not in (step.get("if") or ""):
                dropped.append(step.get("name") or step.get("uses"))
        self.assertEqual(dropped, [])

    def test_check_and_test_detect_is_unconditional(self):
        """Those lanes always check out. A conditional detect could skip the
        output and then skip cargo on a Rust repo, which is a green lane
        that did not build."""
        for name in ("lane-check.yml", "lane-test.yml"):
            detect = named_step(ROOT / ".github/workflows" / name, "Detect Rust workspace")
            self.assertNotIn(
                "if",
                detect,
                f"{name} detect step grew an if; cargo would skip when it is false",
            )


if __name__ == "__main__":
    unittest.main()
