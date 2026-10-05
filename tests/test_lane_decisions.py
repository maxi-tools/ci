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

import ast

# Module-level helpers for the merge-gate event-filter evaluator. Kept out
# of the test class so a future widening of the supported GitHub Actions
# grammar adds a new method here, not a new branch inside a 200-line method.
class _NullSafe(types.SimpleNamespace):
    """SimpleNamespace that returns None for missing attributes.

    GitHub Actions returns null on `event.<missing>` and the comparison
    `null == 'foo'` is false; Python's SimpleNamespace raises AttributeError,
    which would mask the very case the test is checking.
    """
    def __getattr__(self, name: str) -> object:
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            return None

    @classmethod
    def github(cls, event_name: str, event_obj: object) -> "_NullSafe":
        wrapped = _wrap_dict({
            "event_name": event_name,
            "event": event_obj,
            "repository": "maxi-tools/ci",
        })
        if not isinstance(wrapped, _NullSafe):
            raise ValueError("github() expects a dict-shaped event")
        return wrapped


def _wrap_dict(value: object) -> object:
    if isinstance(value, dict):
        return _NullSafe(**{k: _wrap_dict(v) for k, v in value.items()})
    return value


class _Translate:
    """GitHub Actions expression -> Python expression source.

    `&&` -> `and`, `||` -> `or`, `always()` -> True. The result is a small
    expression over the supported operators, not arbitrary user input --
    the test loader reads the bytes straight from the workflow file and the
    test corpus is the workflow itself.
    """
    @staticmethod
    def gha_to_python(expr: str) -> str:
        return (expr.replace("&&", " and ")
                   .replace("||", " or ")
                   .replace("always()", "True"))


class _Walk:
    """Bounded AST walker for the GitHub Actions grammar we use.

    Dispatches per node type so adding a node kind is a new branch on the
    class, not a new `isinstance` deep inside a function. The grammar is
    the union of BoolOp, Compare, Name, Attribute, Constant -- anything
    else raises ValueError, which fails the test loudly instead of
    silently reading whatever `eval` happens to allow.
    """
    @staticmethod
    def evaluate(node: ast.AST, ns: dict) -> object:
        if isinstance(node, ast.Expression):
            return _Walk.evaluate(node.body, ns)
        if isinstance(node, ast.BoolOp):
            return _Walk.boolop(node, ns)
        if isinstance(node, ast.Compare):
            return _Walk.compare(node, ns)
        if isinstance(node, ast.Name):
            return _Walk.name(node, ns)
        if isinstance(node, ast.Attribute):
            return _Walk.attribute(node, ns)
        if isinstance(node, ast.Constant):
            return node.value
        raise ValueError(f"unsupported node: {type(node).__name__}")

    @staticmethod
    def boolop(node: ast.BoolOp, ns: dict) -> bool:
        values = [_Walk.evaluate(v, ns) for v in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise ValueError(f"unsupported BoolOp: {type(node.op).__name__}")

    @staticmethod
    def compare(node: ast.Compare, ns: dict) -> bool:
        left = _Walk.evaluate(node.left, ns)
        for op, right_node in zip(node.ops, node.comparators):
            right = _Walk.evaluate(right_node, ns)
            if isinstance(op, ast.Eq) and left != right:
                return False
            if isinstance(op, ast.NotEq) and left == right:
                return False
            left = right
        return True

    @staticmethod
    def name(node: ast.Name, ns: dict) -> object:
        if node.id == "github":
            return ns["github"]
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        raise ValueError(f"unknown name: {node.id!r}")

    @staticmethod
    def attribute(node: ast.Attribute, ns: dict) -> object:
        value = _Walk.evaluate(node.value, ns)
        return getattr(value, node.attr, None)


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

    def test_a_classifier_skip_classifies_merge_gate_only(self):
        """The rust-touched verdict widens the skip beyond docs.

        A review-gate pin bump is not documentation, so the docs-only
        arm would run every lane for it. The classifier's positive
        `false` must take the same merge-gate-only path the docs arm
        takes, or the saving this gate exists for never arrives.
        """
        body = step_run(LANE_PLAN, "lane-plan", "Classify change scope")
        rc, log, outputs = run_bash(body, {
            "CHANGED_FILES": ".github/workflows/review-gate.yml",
            "RUST_TOUCHED": "false",
            "RUST_TOUCHED_REASON": "none of 1 changed file(s) can reach rustc",
        })
        self.assertEqual(rc, 0, log)
        self.assertEqual(outputs.get("scope"), "merge-gate-only")
        self.assertIn("can reach rustc", outputs.get("scope_reason", ""))

    def test_a_missing_classifier_verdict_runs_every_lane(self):
        """No verdict is not a skip: the default direction is full."""
        body = step_run(LANE_PLAN, "lane-plan", "Classify change scope")
        rc, log, outputs = run_bash(body, {
            "CHANGED_FILES": ".github/workflows/review-gate.yml",
        })
        self.assertEqual(rc, 0, log)
        self.assertEqual(outputs.get("scope"), "full")

    def test_a_true_classifier_verdict_runs_every_lane(self):
        body = step_run(LANE_PLAN, "lane-plan", "Classify change scope")
        rc, log, outputs = run_bash(body, {
            "CHANGED_FILES": ".github/workflows/rust_test.yml",
            "RUST_TOUCHED": "true",
            "RUST_TOUCHED_REASON": "defines the build",
        })
        self.assertEqual(rc, 0, log)
        self.assertEqual(outputs.get("scope"), "full")


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
        if not isinstance(expr, str):
            raise ValueError(f"merge-gate `if:` is not a string: {expr!r}")
        return expr

    @staticmethod
    def _evaluates(expr: str, event_name: str, event_obj: object) -> bool:
        py = _Translate.gha_to_python(expr)
        ns = {"github": _NullSafe.github(event_name, event_obj)}
        return bool(_Walk.evaluate(ast.parse(py, mode="eval"), ns))

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
