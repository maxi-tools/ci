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

  rust-ci `package.should_run`
      was `packaging_impact || main || release/*`, which is TRUE on a
      pull request whose diff touches a Cargo.toml, a Dockerfile or a
      `src/bin/*` path -- that is most Rust PRs. It therefore ran
      `cargo build --workspace --release` in the PR lane, an LTO /
      codegen-units=1 compile whose output is never signed and never
      bundled, so there is nothing anyone can install.

Each test extracts the step's own `run:` body from the workflow and
executes it, so the thing under test is the bytes CI runs. (codacy on
maxi-config#790.)

Run directly: `python3 tests/test_lane_decisions.py`.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
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


# --------------------------------------------------------------------------
# A GitHub-expression evaluator for the `should_run:` gates.
#
# WHY AN EVALUATOR AND NOT A SUBSTRING ASSERTION. The property this file has
# to protect is behavioural: on `pull_request` and `merge_group` the package
# lane must not build with the release profile, and on schedule / tag / release
# refs it must. A `require("github.event_name != 'pull_request'" in expr)`
# assertion is satisfied by that fragment appearing anywhere -- including in a
# comment, in a disjunction that a sibling term already satisfies, or in a
# predicate about a DIFFERENT lane. Every one of those reads green while the
# fleet pays for a full LTO compile on every pull request, which is the exact
# defect the gate exists to prevent. This file's own history is the argument:
# `clippy_templates_use_sccache_and_keyed_dependency_cache` pins a SHA that
# exists in a template AND a fixture, and when Dependabot moved one the lane
# went red for a reason unrelated to what it claimed to test; the family of
# "assert a literal" tests is what makes those failures hard to read.
#
# The grammar is deliberately the smallest one that covers the gates:
# `||`, `&&`, `==`, `!=`, parentheses, single-quoted strings, dotted context
# references, and `startsWith` / `always`. Anything else RAISES rather than
# evaluating to a default, so widening the grammar of a gate is a failure here
# instead of a silent pass -- an unrecognised term is the shape a typo takes
# (`!=` misspelt as `=<` leaves a free term that reads as "some value we did
# not supply", which is how a gate goes quietly inert).
# --------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"""
    \s*(?:
      (?P<string>'(?:[^']|'')*')
    | (?P<lparen>\()
    | (?P<rparen>\))
    | (?P<comma>,)
    | (?P<op>==|!=|&&|\|\|)
    | (?P<term>[A-Za-z_][A-Za-z0-9_.\-]*)
    )
    """,
    re.VERBOSE,
)

_LITERALS = {"true": True, "false": False, "null": None}


def _tokenize(source):
    tokens, i = [], 0
    while i < len(source):
        if source[i].isspace():
            i += 1
            continue
        match = _TOKEN_RE.match(source, i)
        if not match or match.end() == i:
            raise ValueError(f"cannot tokenize gate at {source[i:i + 30]!r}")
        kind = match.lastgroup
        if kind is None:
            raise ValueError(f"tokenized group is unnamed at {source[i:i + 30]!r}")
        tokens.append((kind, match.group(kind)))
        i = match.end()
    return tokens


def _truthy(value):
    """GitHub truthiness. The empty string and `false` are false; so is `null`.

    A bare string is truthy even when it reads like a boolean's name, which is
    why `github.event_name` alone must never be used as a condition.
    """
    if value is None or value is False:
        return False
    if value is True:
        return True
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value != ""
    raise ValueError(f"cannot take the truthiness of {value!r}")


def _gha_equal(left, right):
    """GitHub compares loosely ACROSS types.

    `github.event_name == 'true'` and the boolean `true` compare equal, which is
    why a gate that compares an event name against a boolean literal is not a
    typo GitHub would catch for you.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return _truthy(left) == _truthy(right)
    return str(left).casefold() == str(right).casefold()


class _Gate:
    """or := and ('||' and)* ; and := cmp ('&&' cmp)* ; cmp := prim (op prim)?"""

    def __init__(self, tokens, env):
        self._tokens, self._index, self._env = tokens, 0, env

    def _peek(self):
        return self._tokens[self._index] if self._index < len(self._tokens) else None

    def _take(self):
        token = self._peek()
        if token is None:
            raise ValueError("unexpected end of gate expression")
        self._index += 1
        return token

    def parse_or(self):
        # Parse EVERY term, then combine.
        #
        # NOT `value or self.parse_and()`. Python's `or` does not evaluate its
        # right operand once the left is truthy, and `and` does not evaluate
        # once the left is false, so either spelling leaves the remaining tokens
        # unconsumed -- and `evaluate_gate`'s trailing-token check then reports
        # a grammar gap on a gate this grammar handles perfectly. The bind
        # happens here, on already-parsed values, for that reason.
        value = self.parse_and()
        while self._peek() == ("op", "||"):
            self._take()
            right = self.parse_and()
            value = bool(_truthy(value)) or bool(_truthy(right))
        return value

    def parse_and(self):
        value = self.parse_cmp()
        while self._peek() == ("op", "&&"):
            self._take()
            right = self.parse_cmp()
            value = bool(_truthy(value)) and bool(_truthy(right))
        return value

    def parse_cmp(self):
        left = self.parse_primary()
        token = self._peek()
        if token is not None and token[0] == "op" and token[1] in ("==", "!="):
            operator = self._take()[1]
            right = self.parse_primary()
            equal = _gha_equal(left, right)
            return equal if operator == "==" else not equal
        return left

    def parse_primary(self):
        kind, text = self._take()
        if kind == "lparen":
            value = self.parse_or()
            if self._take() != ("rparen", ")"):
                raise ValueError("unbalanced parenthesis in gate")
            return value
        if kind == "string":
            return text[1:-1].replace("''", "'")
        lowered = text.casefold()
        if lowered in _LITERALS:
            return _LITERALS[lowered]
        if self._peek() == ("lparen", "("):
            return self._call(text)
        if text not in self._env:
            # A term with no supplied value is a hole in the test, not a term
            # that happens to be false. Fail loudly: a gate that reads a context
            # the harness does not model is a gate this test does not cover.
            raise ValueError(f"no value supplied for context {text!r}")
        return self._env[text]

    def _call(self, name):
        self._take()  # the '('
        args = []
        if self._peek() != ("rparen", ")"):
            args.append(self.parse_or())
            while self._peek() == ("comma", ","):
                self._take()
                args.append(self.parse_or())
        if self._take() != ("rparen", ")"):
            raise ValueError(f"unterminated call to {name} in gate")
        if name == "startsWith":
            return str(args[0]).startswith(str(args[1]))
        if name in ("always", "success"):
            return True
        raise ValueError(f"unsupported function {name!r} in a should_run gate")


def evaluate_gate(expression, env):
    """Evaluate a `${{ ... }}` gate under `env`. Raises on anything unmodelled."""
    source = str(expression).strip()
    require(
        source.startswith("${{") and source.endswith("}}"),
        f"not a ${{{{ }}}} expression, so it cannot be gated on: {source!r}",
    )
    gate = _Gate(_tokenize(source[3:-2]), env)
    value = gate.parse_or()
    require(
        gate._index == len(gate._tokens),
        f"trailing tokens left unparsed in gate {source!r}; the grammar this "
        "test models does not cover it, so the gate is NOT covered",
    )
    return _truthy(value)


def require(condition: bool, message: str) -> None:
    """Assertion phrased for a gate, not for a substring.

    The lane gates in this file are read from the PARSED job and
    evaluated, so a failure here means the gate admits an event it
    should refuse -- not that a literal moved.
    """
    if not condition:
        raise AssertionError(message)


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


class ReleaseProfileStaysOffThePullRequestPath(unittest.TestCase):
    """rust-ci's package lane must not build the optimised profile on a PR.

    Read from the PARSED job and EVALUATED, never substring-matched. A
    `require("github.event_name != 'pull_request'" in gate)` assertion is
    satisfied by that fragment appearing anywhere -- in a comment, in a
    disjunction a sibling term already satisfies, or in a predicate about a
    different lane. Each of those reads green while every consumer pays for
    an LTO compile per PR, which is the whole cost this gate exists to
    remove.

    `packaging_impact` is varied deliberately. It is the one input that was
    SUFFICIENT on its own to trigger the build on a PR -- ci/packaging-paths
    .toml marks Cargo.toml, */Cargo.toml, src/bin/*, Dockerfile, packaging/*
    and release/* as packaging-impact, so most Rust PRs qualify -- and a gate
    that only refuses when it is false has refused nothing.
    """

    def gate(self, lane: str) -> str:
        doc = yaml.safe_load(RUST_CI.read_text(encoding="utf-8"))
        return doc["jobs"][lane]["with"]["should_run"]

    def run_gate(self, gate: str, event: str, ref: str, impact: str = "false") -> bool:
        return evaluate_gate(
            gate,
            {
                "needs.plan.outputs.packaging_impact": impact,
                "github.event_name": event,
                "github.ref": ref,
            },
        )

    # A PR's ref is refs/pull/<n>/merge and a merge-queue head is the queue's
    # temporary branch, refs/heads/gh-readonly-queue/mg/pr-<n>-<sha>. Neither
    # is the base branch, which is why a gate recognising only
    # refs/heads/main would exclude both BY ACCIDENT. That is a property of the
    # refs those events happen to carry, not of the gate, so both are asserted
    # with their real shapes and the event-name clause is asserted to exist.
    SCENARIOS = [
        # (label, event, ref, packaging_impact, expected)
        ("PR, no packaging impact", "pull_request", "refs/pull/4821/merge", "false", False),
        ("PR, packaging impact", "pull_request", "refs/pull/4821/merge", "true", False),
        ("merge_group, packaging impact", "merge_group",
         "refs/heads/gh-readonly-queue/mg/pr-4821-abcdef", "true", False),
        ("merge_group, no packaging impact", "merge_group",
         "refs/heads/gh-readonly-queue/mg/pr-4821-abcdef", "false", False),
        ("push to main", "push", "refs/heads/main", "false", True),
        ("push to a release branch", "push", "refs/heads/release/1.2", "false", True),
        ("schedule (nightly)", "schedule", "refs/heads/main", "false", True),
        ("workflow_dispatch on main", "workflow_dispatch", "refs/heads/main", "false", True),
    ]

    def test_the_package_lane_refuses_both_pr_events(self):
        gate = self.gate("package")
        for label, event, ref, impact, expected in self.SCENARIOS:
            with self.subTest(scenario=label):
                actual = self.run_gate(gate, event, ref, impact)
                self.assertEqual(
                    actual, expected,
                    f"package.should_run on {label} (event={event}, ref={ref}, "
                    f"packaging_impact={impact}) was {actual}. Gate: {gate}. "
                    "A release build on this path is a full LTO compile of an "
                    "artifact nobody signs, bundles, or can install.",
                )

    def test_the_refusal_is_by_event_name_not_by_the_refs_those_events_happen_to_carry(self):
        """The guard must name merge_group explicitly.

        A merge-queue head ref matches none of the disjuncts today, so a gate
        filtering only `pull_request` already excludes merge_group. That is a
        coincidence of the queue ref's shape. If the queue ever renames its
        branch -- and it has, `gh-readonly-queue` is itself recent -- the
        explicit clause is what still holds, and its absence is invisible until
        the queue then pays for an optimised build per merge.
        """
        gate = self.gate("package")
        require(
            "github.event_name != 'pull_request'" in gate,
            f"package.should_run must exclude pull_request by EVENT NAME: {gate}",
        )
        require(
            "github.event_name != 'merge_group'" in gate,
            f"package.should_run must exclude merge_group by EVENT NAME, not "
            f"rely on the queue's branch ref not matching the disjuncts: {gate}",
        )

    def test_signing_and_bundling_stay_on_trusted_refs(self):
        """Same property for the two lanes downstream of package.

        They are gated on the ref rather than the event name, which happens to
        exclude PRs only because a PR's ref is refs/pull/*. If someone
        "simplified" that to an event-name test, signing and bundling would
        start running on a fork's head.
        """
        for lane in ("sign-publish", "release-verify"):
            gate = self.gate(lane)
            for label, event, ref, expected in [
                ("PR", "pull_request", "refs/pull/4821/merge", False),
                ("merge_group", "merge_group",
                 "refs/heads/gh-readonly-queue/mg/pr-4821-abcdef", False),
                ("push to main", "push", "refs/heads/main", True),
                ("push to a release branch", "push", "refs/heads/release/1.2", True),
                ("schedule", "schedule", "refs/heads/main", True),
            ]:
                with self.subTest(lane=lane, scenario=label):
                    actual = self.run_gate(gate, event, ref)
                    self.assertEqual(
                        actual, expected,
                        f"{lane}.should_run on {label} (event={event}, "
                        f"ref={ref}) was {actual}, expected {expected}. "
                        f"Gate: {gate}. Signing and bundling run on trusted "
                        "refs only.",
                    )

    def test_exactly_one_step_in_the_lane_chain_builds_the_release_profile(self):
        """A whole-directory scan, not a check of one named file.

        The waste being removed is a property of "the PR lane", not of
        lane-package.yml. A second `--release` added to lane-check.yml or
        lane-test.yml tomorrow would reinstate the entire cost with every
        other assertion here still green.

        `cargo` is matched NOT followed by `-`, `_` or `.`, so it does not
        match `cargo-target-v1--...--workspace--release--...`: that is
        lane-package's artifact-IDENTITY step, whose body spells an artifact
        name and invokes no compiler. Comment lines are skipped for the same
        reason a shell would skip them.
        """
        lane_dir = RUST_CI.parent
        builders = []
        for lane_path in sorted(lane_dir.glob("lane-*.yml")):
            doc = yaml.safe_load(lane_path.read_text(encoding="utf-8"))
            for job_name, job in (doc.get("jobs") or {}).items():
                for step in job.get("steps") or []:
                    body = step.get("run")
                    if not isinstance(body, str):
                        continue
                    for line in body.splitlines():
                        stripped = line.strip()
                        if stripped.startswith("#"):
                            continue
                        if re.search(
                            r"\bcargo(?![-_.])[^\n]*(--release|(?:^|\s)-r(?:\s|$))",
                            stripped,
                        ):
                            builders.append(
                                f"{lane_path.name}:{job_name}:{step.get('name')}"
                            )
        self.assertEqual(
            builders, ["lane-package.yml:lane-package:Build release artifacts"],
            "the optimised profile must be built by exactly one lane in "
            f"exactly one step, and that step must be the package lane: {builders}",
        )


if __name__ == "__main__":
    unittest.main()
