#!/usr/bin/env python3
"""The two lane decisions that can silently skip testing, RUN not read.

Both were fail-open when they shipped, and both are invisible to
actionlint: it checks that a workflow parses, not what its shell decides.

  lane-plan `Classify change scope`
      `docs_only` starts true, so an empty changed-file list -- a failed
      lookup -- classified as "documentation only" and skipped check and
      test in every consumer. The same repository refuses exactly this
      inference elsewhere by name (check-owned-files.py,
      check-intra-org-pins.py: "an empty list is a failed lookup, not a
      clean PR").

  rust-ci `Aggregate lane results`
      failed on `failure` and `cancelled` only, so any other result
      passed. This job is the SOLE required context under the new
      ruleset, which makes an unrecognised value a green gate over a
      red lane.

Each test extracts the step's own `run:` body from the workflow and
executes it, so the thing under test is the bytes CI runs. (codacy on
maxi-config#790.)

Run directly: `python3 tests/test_lane_decisions.py`.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import types
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
LANE_PLAN = ROOT / ".github/workflows/lane-plan.yml"
RUST_CI = ROOT / ".github/workflows/rust-ci.yml"


def step_run(path: pathlib.Path, job: str, step_name: str) -> str:
    """The `run:` body of one named step, or fail loudly."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = doc["jobs"][job]["steps"]
    for step in steps:
        if step.get("name") == step_name:
            assert "run" in step, f"{step_name} has no run: block"
            return step["run"]
    raise AssertionError(
        f"{path.name} has no step named {step_name!r}; this test reads that "
        "step directly and a rename must not silently stop testing it")


def run_bash(body: str, env: dict) -> tuple[int, str, dict]:
    """Execute a step body with a real GITHUB_OUTPUT, return its outputs."""
    with tempfile.TemporaryDirectory() as tmp:
        out_file = pathlib.Path(tmp) / "out"
        out_file.touch()
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-c", body],
            env={"PATH": os.environ.get("PATH", ""),
                 "GITHUB_OUTPUT": str(out_file), **env},
            capture_output=True, text=True, timeout=30,
        )
        outputs = {}
        for line in out_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                outputs[key] = value
        return proc.returncode, proc.stdout + proc.stderr, outputs


class ClassifyChangeScope(unittest.TestCase):
    def scope(self, changed_files: str):
        body = step_run(LANE_PLAN, "lane-plan", "Classify change scope")
        rc, log, outputs = run_bash(body, {"CHANGED_FILES": changed_files})
        self.assertEqual(rc, 0, log)
        return outputs.get("scope"), outputs.get("scope_reason", "")

    def test_an_empty_list_is_a_failed_lookup_not_a_docs_only_pr(self):
        """THE defect: nothing to classify must not mean nothing to test."""
        for empty in ("", "\n", "   \n\n"):
            with self.subTest(value=repr(empty)):
                scope, reason = self.scope(empty)
                self.assertEqual(scope, "full")
                self.assertIn("lookup returned nothing", reason)

    def test_a_docs_only_change_still_skips(self):
        """The optimisation must survive the fix, or it is not a fix."""
        scope, reason = self.scope("README.md\ndocs/design.md\nCHANGELOG.md")
        self.assertEqual(scope, "merge-gate-only")
        self.assertIn("documentation", reason)

    def test_one_code_path_among_docs_is_full(self):
        scope, reason = self.scope("README.md\nsrc/main.rs\ndocs/x.md")
        self.assertEqual(scope, "full")
        self.assertIn("src/main.rs", reason)

    def test_a_lockfile_is_not_documentation(self):
        scope, _ = self.scope("Cargo.lock")
        self.assertEqual(scope, "full")

    def test_a_dotfile_and_a_workflow_are_not_documentation(self):
        for path in (".github/workflows/rust-ci.yml", "ci/runner-routing.toml"):
            with self.subTest(path=path):
                self.assertEqual(self.scope(path)[0], "full")


class AggregateLaneResults(unittest.TestCase):
    def aggregate(self, needs: dict):
        body = step_run(RUST_CI, "merge-gate", "Aggregate lane results")
        return run_bash(body, {"MERGE_GATE_NEEDS": json.dumps(needs)})

    def test_every_lane_successful_passes(self):
        rc, log, _ = self.aggregate({"plan": {"result": "success"},
                                     "check": {"result": "success"}})
        self.assertEqual(rc, 0, log)

    def test_a_skipped_lane_is_fine(self):
        rc, log, _ = self.aggregate({"check": {"result": "skipped"},
                                     "test": {"result": "success"}})
        self.assertEqual(rc, 0, log)
        self.assertIn("skipped=['check']", log)

    def test_failure_and_cancellation_fail_the_gate(self):
        for result in ("failure", "cancelled"):
            with self.subTest(result=result):
                rc, log, _ = self.aggregate({"check": {"result": result}})
                self.assertEqual(rc, 1)
                self.assertIn("::error::Required lane 'check'", log)

    def test_an_unrecognised_result_fails_rather_than_passing(self):
        """THE defect: the old denylist passed anything it did not name."""
        for result in ("timed_out", "action_required", "neutral", "stale", ""):
            with self.subTest(result=result):
                rc, log, _ = self.aggregate({"check": {"result": result}})
                self.assertEqual(rc, 1, log)
                self.assertIn("::error::Required lane 'check'", log)

    def test_a_lane_with_no_result_key_fails(self):
        """A lane that never reported is not a lane that passed."""
        rc, log, _ = self.aggregate({"check": {}})
        self.assertEqual(rc, 1, log)
        self.assertIn("None", log)

    def test_no_lanes_at_all_passes_but_says_so(self):
        """`needs` empty is the merge-gate-only shape, not an error."""
        rc, log, _ = self.aggregate({})
        self.assertEqual(rc, 0, log)
        self.assertIn("skipped=[]", log)


# `merge-gate` is the SOLE required context under the post-merge-gate ruleset.
# The job's `if:` decides whether the rule even exists on a given event --
# skip it on the wrong event and the ruleset is structurally weaker than the
# lanes it is supposed to replace. actionlint checks expression syntax, not
# what it evaluates against. (codacy on maxi-config#790; the same reasoning
# made this file ship its first two tests.)
#
# `always()` is the force-run-on-upstream-failure override, not a boolean
# value: with it, the job runs even when its needs failed, as long as the
# rest of the expression is true. Treat it as True in the boolean evaluation
# so the test reads the EVENT filter, not the failure override -- the
# override is asserted separately.
class MergeGateEventFilter(unittest.TestCase):
    @staticmethod
    def _if_expr() -> str:
        doc = yaml.safe_load(RUST_CI.read_text(encoding="utf-8"))
        expr = doc["jobs"]["merge-gate"]["if"]
        assert isinstance(expr, str), f"merge-gate `if:` is not a string: {expr!r}"
        return expr

    @staticmethod
    def _evaluates(expr: str, event_name: str, event_obj: object) -> bool:
        # GitHub Actions uses `&&` / `||`. Python uses `and` / `or`.
        py = (expr.replace("&&", " and ")
                  .replace("||", " or ")
                  # `always()` is the upstream-failure override; for the event
                  # filter it is a no-op -- evaluate the boolean condition
                  # the rule would evaluate against.
                  .replace("always()", "True"))
        # Recursively wrap dicts in SimpleNamespace so attribute-style
        # property access (`github.event.pull_request.head.repo.full_name`)
        # works in the eval'd expression. The wrapper also tolerates missing
        # attributes -- GitHub Actions returns null on `event.<missing>` and
        # compares false against any string; Python's SimpleNamespace raises
        # AttributeError, which would mask the very case the test is checking.
        class _NullSafe(types.SimpleNamespace):
            def __getattr__(self, name: str) -> object:
                try:
                    return object.__getattribute__(self, name)
                except AttributeError:
                    return None
        def wrap(value: object) -> object:
            if isinstance(value, dict):
                return _NullSafe(**{k: wrap(v) for k, v in value.items()})
            return value
        ns = {
            "__builtins__": {},
            "True": True, "False": False, "None": None,
            "github": wrap({
                "event_name": event_name,
                "event": event_obj,
                "repository": "maxi-tools/ci",
            }),
        }
        return bool(eval(py, ns))

    def gate_should_run(self, event_name: str, event_obj: object) -> bool:
        return self._evaluates(self._if_expr(), event_name, event_obj)

    def test_pull_request_same_repo_runs(self):
        """Existing fork-tripwire arm still passes a same-repo PR."""
        event = {"pull_request": {"head": {"repo": {"full_name": "maxi-tools/ci"}}}}
        self.assertTrue(self.gate_should_run("pull_request", event))

    def test_pull_request_fork_does_not_run(self):
        """The fork tripwire still filters a fork PR -- the new merge_group
        arm must not widen this."""
        event = {"pull_request": {"head": {"repo": {"full_name": "maxi-tools/attacker"}}}}
        self.assertFalse(self.gate_should_run("pull_request", event))

    def test_merge_group_checks_requested_runs(self):
        """The merge-queue caller asks this workflow to emit the required
        `merge-gate / merge-gate` context on `merge_group` with action
        `checks_requested`. Without this arm the job is skipped and the
        ruleset sees no verdict."""
        event = {"action": "checks_requested"}
        self.assertTrue(self.gate_should_run("merge_group", event))

    def test_merge_group_other_actions_do_not_run(self):
        """`merge_group` actions besides `checks_requested` are merge-queue
        lifecycle events. The aggregate check run must not be created for
        them: it would be a required-context check run with no upstream
        lane results to aggregate, and it would block the queue."""
        for action in ("created", "merged", "pushed", "removed", "deleted"):
            with self.subTest(action=action):
                event = {"action": action}
                self.assertFalse(self.gate_should_run("merge_group", event),
                                 f"merge_group action={action!r} must not run merge-gate")

    def test_merge_group_with_no_action_does_not_run(self):
        """Defensive: a malformed merge_group payload without `action` must
        not mint the required check run."""
        event = {}
        self.assertFalse(self.gate_should_run("merge_group", event))

    def test_unrelated_event_names_do_not_run(self):
        """A `push` or `workflow_dispatch` event must not run merge-gate.
        The merge-gate is the PR/merge-group aggregate, not a push-triggered one."""
        for event_name in ("push", "workflow_dispatch", "schedule", "release"):
            with self.subTest(event_name=event_name):
                event = {"action": "checks_requested"}  # even with a matching action
                self.assertFalse(self.gate_should_run(event_name, event))

    def test_if_expression_uses_always_override(self):
        """The upstream-failure override must still be present in the
        expression. Without it, a failing `check`/`test` skips merge-gate
        and the required context vanishes -- which is the failure mode
        this whole job exists to fix. actionlint cannot catch a deleted
        `always()`; this test does."""
        self.assertIn("always()", self._if_expr())


if __name__ == "__main__":
    unittest.main()
