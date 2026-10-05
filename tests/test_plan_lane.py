#!/usr/bin/env python3
"""Which SHA the plan lane diffs from, per event shape.

`Collect changed files` decides the DIFF BASE for the whole lane chain:
scope classification, packaging impact and every downstream lane's
"what changed" answer are all read off this list. Getting the base wrong
does not fail loudly -- it produces a plausible list of the wrong files,
and a docs-looking diff silently classifies as `merge-gate-only` and
skips check and test. So the base is worth its own test, executed rather
than asserted on the YAML.

THE defect this pins (ci#83 codex P1, PRRT_kwDOUUTR_s6o0w-D):

    The step read its base from `$BASE_REF` alone, which rust-ci.yml fills
    with `${{ github.event.before || 'origin/main' }}`. A `merge_group`
    payload has no `before` field at all, so every merge-queue run fell to
    `origin/main`. GitHub supports queues targeting `release/**`, and on
    such a queue `origin/main` is not an ancestor of the queue head at
    all -- `git diff` against it yields a path list that has nothing to do
    with the group being tested. The merge_group payload carries its own
    base at `github.event.merge_group.base_sha`; the step now reads it.

Each test extracts the step's own `run:` body from the workflow,
substitutes the `${{ }}` expressions, and executes it inside a REAL git
repository with real commits -- so the thing under test is the bytes CI
runs against the bytes git answers, not a transcription of them.

Run directly: `uv run --with pyyaml python3 tests/test_plan_lane.py`
(pyyaml is not in the system interpreter; `uv run --with` supplies it).
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
LANE_PLAN = ROOT / ".github/workflows/lane-plan.yml"

# Git's empty tree. The step substitutes it for an all-zero SHA, and a
# test that wants "no base at all" wants this.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
ZERO_SHA = "0" * 40

EXPR = re.compile(r"\$\{\{\s*([^}]*?)\s*\}\}")


def step(path: pathlib.Path, job: str, step_name: str) -> dict:
    """One named step out of a workflow, or fail loudly."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    for candidate in doc["jobs"][job]["steps"]:
        if candidate.get("name") == step_name:
            return candidate
    raise AssertionError(
        f"{path.name} has no step named {step_name!r}; this test reads that "
        "step directly and a rename must not silently stop testing it")


def resolve_expr(expr: str, substitutions: dict) -> str:
    """One `${{ }}` expression's value, honouring a `|| fallback`.

    The fallback matters: `MERGE_GROUP_BASE_SHA` is spelled
    `${{ github.event.merge_group.base_sha || '' }}`, and if the test
    substituted the literal text instead of the empty string, the
    "no merge_group payload" case would exercise a value no runner ever
    sees -- the tests would pass against a shape that cannot occur.
    """
    if "||" not in expr:
        return str(substitutions.get(expr.strip(), ""))
    left, _, default = expr.partition("||")
    value = str(substitutions.get(left.strip(), ""))
    # GitHub's `||` is falsy-fallback: an empty string takes the default
    # too, which is what keeps the unset case honest.
    return value if value else default.strip().strip("'\"")


def step_env(spec: dict, substitutions: dict) -> dict:
    """The step's `env:`, with every `${{ }}` expression resolved."""
    return {key: EXPR.sub(lambda m: resolve_expr(m.group(1), substitutions),
                          raw)
            for key, raw in spec.items()}


def render(body: str, substitutions: dict) -> str:
    """Substitute `${{ }}` expressions in a `run:` body."""
    return EXPR.sub(lambda m: resolve_expr(m.group(1), substitutions), body)


def git(repo: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


class PlanBaseRef(unittest.TestCase):
    """The base SHA the diff is taken from, for each event shape."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = pathlib.Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--quiet", "--initial-branch=main")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "test")

        # Commit 1: the queue's BASE. A docs-only tree, so a diff taken
        # from the wrong base is visible as scope-shaped rather than as an
        # empty list.
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "design.md").write_text("d\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "base")
        self.base_sha = git(self.repo, "rev-parse", "HEAD")

        # Commit 2: the queue's HEAD. Adds one source file -- exactly the
        # kind of change that must classify `full`, never `merge-gate-only`.
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.rs").write_text("fn main() {}\n",
                                                   encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "head")
        self.head_sha = git(self.repo, "rev-parse", "HEAD")

        self.addCleanup(self._tmp.cleanup)

    # -- helpers ------------------------------------------------------

    def collect(self, *, base_ref: str, merge_group_base_sha: str,
                event_name: str = "merge_group") -> tuple[int, str, str]:
        """Execute the real step body in the real repo; return its files."""
        spec = step(LANE_PLAN, "lane-plan", "Collect changed files")
        env = step_env(spec["env"], {
            "github.event.merge_group.base_sha": merge_group_base_sha,
            "inputs.base_ref": base_ref,
        })
        body = render(spec["run"], {
            "github.event_name": event_name,
            "github.repository": "maxi-tools/example",
            "github.event.pull_request.number": "1",
            "github.sha": self.head_sha,
        })
        with tempfile.TemporaryDirectory() as out_dir:
            out_file = pathlib.Path(out_dir) / "out"
            out_file.touch()
            proc = subprocess.run(
                ["bash", "--noprofile", "--norc", "-c", body],
                cwd=self.repo,
                env={"PATH": os.environ.get("PATH", ""),
                     "HOME": str(self.repo),
                     "GITHUB_OUTPUT": str(out_file),
                     "GIT_CONFIG_GLOBAL": "/dev/null",
                     **env},
                capture_output=True, text=True, timeout=60,
            )
            files = ""
            seen_key = False
            delimiter = ""
            for line in out_file.read_text(encoding="utf-8").splitlines():
                if not seen_key:
                    if line.startswith("changed_files<<"):
                        seen_key = True
                        delimiter = line.partition("<<")[2]
                    continue
                if line == delimiter:
                    break
                files = files + line + "\n"
            return proc.returncode, proc.stdout + proc.stderr, files

    # -- the defect ---------------------------------------------------

    def test_a_merge_group_run_diffs_from_the_merge_group_base(self):
        """THE defect: `before` is absent on a merge_group payload, so the
        lane's base fell through to `origin/main` -- which on a release/**
        queue is not an ancestor of the head at all."""
        rc, log, files = self.collect(
            base_ref="origin/main",  # what the caller resolves to today
            merge_group_base_sha=self.base_sha,
        )
        self.assertEqual(rc, 0, log)
        self.assertEqual(files.split(), ["src/main.rs"])

    def test_the_merge_group_base_wins_even_when_a_base_ref_is_supplied(self):
        """A caller that learned to pass its own base must not be able to
        talk the lane out of the payload's base: the merge group knows its
        own parent, and on a release queue the caller's is the wrong
        repository line entirely."""
        rc, log, files = self.collect(
            base_ref="origin/release/2.0",
            merge_group_base_sha=self.base_sha,
        )
        self.assertEqual(rc, 0, log)
        self.assertEqual(files.split(), ["src/main.rs"])

    def test_a_docs_only_merge_group_is_still_a_docs_only_diff(self):
        """The fix must not widen every queue diff into `full`: when the
        group really is docs-only, the honest list is docs-only."""
        (self.repo / "CHANGELOG.md").write_text("## unreleased\n",
                                                encoding="utf-8")
        (self.repo / "src" / "main.rs").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--quiet", "-m", "docs only")
        self.head_sha = git(self.repo, "rev-parse", "HEAD")
        rc, log, files = self.collect(base_ref="origin/main",
                                      merge_group_base_sha=self.base_sha)
        self.assertEqual(rc, 0, log)
        self.assertEqual(sorted(files.split()), ["CHANGELOG.md"])

    # -- regressions the fix must not cause ----------------------------

    def test_a_push_run_still_uses_the_caller_base_ref(self):
        """`push` has a real `before`; nothing about it changed."""
        rc, log, files = self.collect(base_ref=self.base_sha,
                                      merge_group_base_sha="",
                                      event_name="push")
        self.assertEqual(rc, 0, log)
        self.assertEqual(files.split(), ["src/main.rs"])

    def test_a_workflow_dispatch_run_still_uses_the_caller_base_ref(self):
        rc, log, files = self.collect(base_ref=self.base_sha,
                                      merge_group_base_sha="",
                                      event_name="workflow_dispatch")
        self.assertEqual(rc, 0, log)
        self.assertEqual(files.split(), ["src/main.rs"])

    def test_an_all_zero_base_still_falls_back_to_the_empty_tree(self):
        """Branch creation pushes `0000...`; the step has always handled
        that and the new branch sits above it, not across it."""
        rc, log, files = self.collect(base_ref=ZERO_SHA,
                                      merge_group_base_sha="",
                                      event_name="push")
        self.assertEqual(rc, 0, log)
        # Drawn from the empty tree, so EVERY tracked file is changed.
        self.assertIn("README.md", files.split())
        self.assertIn("src/main.rs", files.split())


if __name__ == "__main__":
    unittest.main()
