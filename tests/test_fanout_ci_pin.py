#!/usr/bin/env python3
"""fanout_ci_pin.py: the run must DO what its summary says it did.

From ci#24 until ci#29, fanout-ci-pin.yml passed `--json` on every run and
`--json` returned before `_execute`. Each run planned ~48 consumers, opened
no PR, and ended "fan-out completed; see step summary for the per-consumer
plan". The contract calls the fan-out PR the inert-detector; the detector
was inert, and nothing measured that. These tests hold the script to one
rule: every consumer it looked at ends in exactly one accounted-for
outcome, and the report is a partition of those outcomes, not a count of
the happy path.

Run directly (`python3 tests/test_fanout_ci_pin.py`); self-check.yml does.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / ".github" / "scripts" / "fanout_ci_pin.py"

spec = importlib.util.spec_from_file_location("fanout_ci_pin", SCRIPT)
fp = importlib.util.module_from_spec(spec)
# The script's dataclasses resolve string annotations through sys.modules;
# a module loaded by path is not registered there unless we do it.
sys.modules[spec.name] = fp
spec.loader.exec_module(fp)

TIP = "a" * 40
OLD = "b" * 40


def run_main(*argv, token="t0k"):
    """Run main() with argv; return (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    env = {"GH_TOKEN": token} if token else {}
    with mock.patch.object(sys, "argv", ["fanout_ci_pin.py", *argv]), \
         mock.patch.dict(os.environ, env, clear=False), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fp.main()
    return rc, out.getvalue(), err.getvalue()


def rows(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def no_network(*_a, **_k):
    raise AssertionError("a subprocess ran; the test meant to stay offline")


class JsonModeExecutes(unittest.TestCase):
    """The defect: `--json` printed the plan and returned."""

    def test_json_mode_opens_the_pr_it_reports(self):
        opened = []

        def fake_open_pr(**kw):
            opened.append(kw["consumer"].name)
            return ("opened", "https://github.com/maxi-tools/x/pull/1")

        with mock.patch.object(fp, "_fetch_consumer_pin",
                               lambda c, token: {"review-gate-reusable": OLD}), \
             mock.patch.object(fp, "_open_pr", fake_open_pr):
            rc, out, _ = run_main("--tip", TIP, "--consumer-repo", "maxi-tools/x",
                                  "--json")
        self.assertEqual(opened, ["maxi-tools/x"],
                         "--json must not return before the plan is executed")
        self.assertEqual(rc, 0)
        (row,) = rows(out)
        self.assertEqual(row["outcome"], "opened")
        self.assertEqual(row["pr_url"], "https://github.com/maxi-tools/x/pull/1")

    def test_dry_run_touches_nothing_and_says_so(self):
        with mock.patch.object(fp, "_fetch_consumer_pin",
                               lambda c, token: {"review-gate-reusable": OLD}), \
             mock.patch.object(fp, "_gh", no_network), \
             mock.patch.object(fp, "_run", no_network):
            rc, out, _ = run_main("--tip", TIP, "--consumer-repo", "maxi-tools/x",
                                  "--dry-run", "--json", token=None)
        self.assertEqual(rc, 0)
        (row,) = rows(out)
        self.assertEqual(row["outcome"], "dry-run")
        self.assertIsNone(row["pr_url"])

    def test_a_consumer_already_at_tip_is_not_reopened(self):
        with mock.patch.object(fp, "_fetch_consumer_pin",
                               lambda c, token: {"review-gate-reusable": TIP}), \
             mock.patch.object(fp, "_open_pr", no_network):
            rc, out, _ = run_main("--tip", TIP, "--consumer-repo", "maxi-tools/x",
                                  "--json")
        self.assertEqual(rc, 0)
        (row,) = rows(out)
        self.assertEqual(row["outcome"], "already")


class EveryConsumerIsAccountedFor(unittest.TestCase):
    """A consumer the run could not handle is a row, not an omission."""

    def _two(self, pins, open_pr):
        with mock.patch.object(fp, "_fetch_consumer_pin", pins), \
             mock.patch.object(fp, "_open_pr", open_pr):
            return run_main("--tip", TIP,
                            "--consumer-repo", "maxi-tools/bad",
                            "--consumer-repo", "maxi-tools/good", "--json")

    def test_an_unreadable_pin_is_a_row_and_fails_the_run(self):
        def pins(c, token):
            if c.name.endswith("/bad"):
                raise RuntimeError("HTTP 403")
            return {"review-gate-reusable": OLD}

        rc, out, err = self._two(pins, lambda **kw: ("opened", "u"))
        by = {r["consumer"]: r for r in rows(out)}
        self.assertEqual(by["maxi-tools/bad"]["outcome"], "unreadable")
        self.assertIn("HTTP 403", by["maxi-tools/bad"]["detail"])
        self.assertEqual(by["maxi-tools/good"]["outcome"], "opened",
                         "the readable consumer must still be fanned out")
        self.assertEqual(rc, 1, "an unreadable consumer is not an all-clear")

    def test_one_failed_push_does_not_stop_the_rest(self):
        def open_pr(**kw):
            if kw["consumer"].name.endswith("/bad"):
                raise RuntimeError("command failed (rc=128): git push")
            return ("opened", "u")

        rc, out, _ = self._two(lambda c, token: {"review-gate-reusable": OLD},
                               open_pr)
        by = {r["consumer"]: r for r in rows(out)}
        self.assertEqual(by["maxi-tools/bad"]["outcome"], "failed")
        self.assertIn("git push", by["maxi-tools/bad"]["detail"])
        self.assertEqual(by["maxi-tools/good"]["outcome"], "opened")
        self.assertEqual(rc, 1)

    def test_a_non_sha_pin_and_a_missing_pin_are_rows(self):
        def pins(c, token):
            if c.name.endswith("/bad"):
                return {"review-gate-reusable": "main"}
            return {}

        rc, out, _ = self._two(pins, no_network)
        by = {r["consumer"]: r for r in rows(out)}
        self.assertEqual(by["maxi-tools/bad"]["outcome"], "not-a-sha")
        self.assertEqual(by["maxi-tools/good"]["outcome"], "no-pin")
        self.assertEqual(rc, 0, "neither is a failure of THIS run")

    def test_the_summary_line_partitions_the_rows(self):
        def pins(c, token):
            if c.name.endswith("/bad"):
                raise RuntimeError("nope")
            return {"review-gate-reusable": OLD}

        rc, out, err = self._two(pins, lambda **kw: ("reused", "u"))
        self.assertEqual(len(rows(out)), 2)
        self.assertIn("fan-out: 2 consumer row(s): 1 reused, 1 unreadable", err)

    def test_every_outcome_the_script_can_emit_is_declared(self):
        """OUTCOMES is the schema the workflow's post-summary step reads."""
        src = SCRIPT.read_text(encoding="utf-8")
        for name in ("opened", "reused", "already", "dry-run", "failed",
                     "unreadable", "opt-out", "no-pin", "not-a-sha"):
            self.assertIn(name, fp.OUTCOMES)
            self.assertIn(repr(name), src, f"{name!r} is declared but never emitted")


if __name__ == "__main__":
    unittest.main()