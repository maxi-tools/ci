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
import tempfile
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
                     "unreadable", "opt-out", "no-pin", "not-a-sha", "owned-sync"):
            self.assertIn(name, fp.OUTCOMES)
            self.assertIn(repr(name), src, f"{name!r} is declared but never emitted")


class OneBranchPerConsumer(unittest.TestCase):
    """_open_pr keeps ONE PR per consumer and moves it forward.

    The first cut keyed the branch on the tip sha, so each tip opened a
    second PR beside the last and closed nothing (49 + 49 after ci#31,
    run 35504925677 cancelled by hand). `_gh` and `_push_pin_branch` are
    recorded, not run.
    """

    def _drive(self, open_prs):
        calls = []

        def gh(args, *, token):
            calls.append(args)
            if args[:2] == ["api", "--paginate"]:
                return "\n".join(json.dumps([p["number"], p["url"], p["headRefName"]])
                                 for p in open_prs)
            if args[:2] == ["pr", "create"]:
                return "https://github.com/maxi-tools/x/pull/9\n"
            return ""

        pushes = []
        merges = []
        with mock.patch.object(fp, "_gh", gh), \
             mock.patch.object(fp, "_push_pin_branch",
                               lambda c, **kw: pushes.append(kw["head_ref"])), \
             mock.patch.object(fp, "_enable_automerge",
                               lambda c, url, **kw: merges.append(url)):
            outcome, url = fp._open_pr(
                consumer=fp.Consumer("maxi-tools/x"), workflow_file="review-gate-reusable",
                old_ref=OLD, new_ref=TIP, tip_sha=TIP, token="t", dry_run=False)
        self.assertEqual(merges, [url])
        return outcome, url, calls, pushes

    def test_no_open_pr_creates_one_on_the_stable_branch(self):
        outcome, url, calls, pushes = self._drive([])
        self.assertEqual((outcome, url), ("opened", "https://github.com/maxi-tools/x/pull/9"))
        self.assertEqual(pushes, [fp.HEAD_REF])
        create = next(c for c in calls if c[:2] == ["pr", "create"])
        self.assertIn(f"maxi-tools:{fp.HEAD_REF}", create)
        self.assertFalse(any(c[:2] == ["pr", "close"] for c in calls))

    def test_an_open_pr_on_the_stable_branch_is_moved_not_duplicated(self):
        ours = {"number": 4, "url": "https://github.com/maxi-tools/x/pull/4",
                "headRefName": fp.HEAD_REF}
        outcome, url, calls, pushes = self._drive([ours])
        self.assertEqual((outcome, url), ("reused", ours["url"]))
        self.assertEqual(pushes, [fp.HEAD_REF], "the branch is force-pushed to the new tip")
        self.assertFalse(any(c[:2] == ["pr", "create"] for c in calls), "no second PR")
        edit = next(c for c in calls if c[:3] == ["api", "--method", "PATCH"])
        self.assertEqual(edit[3], "repos/maxi-tools/x/pulls/4")
        self.assertIn(f"title=ci: advance pin to {TIP[:12]} (review-gate-reusable)", edit)

    def test_legacy_per_sha_prs_are_closed_as_superseded(self):
        legacy = {"number": 2, "url": "https://github.com/maxi-tools/x/pull/2",
                  "headRefName": "ci/fanout-660e29c41e4d"}
        unrelated = {"number": 3, "url": "u3", "headRefName": "feat/thing"}
        outcome, url, calls, _ = self._drive([legacy, unrelated])
        self.assertEqual(outcome, "opened")
        closes = [c for c in calls if c[:2] == ["pr", "close"]]
        self.assertEqual([c[2] for c in closes], ["2"], "only the legacy fan-out PR")
        self.assertIn(url, closes[0][closes[0].index("--comment") + 1])
        order = [c[1] for c in calls if c[0] == "pr"]
        self.assertLess(order.index("create"), order.index("close"),
                        "the replacement exists before the old one is closed")

    def test_dry_run_calls_nothing(self):
        with mock.patch.object(fp, "_gh", no_network), \
             mock.patch.object(fp, "_push_pin_branch", no_network):
            self.assertEqual(
                fp._open_pr(consumer=fp.Consumer("maxi-tools/x"),
                            workflow_file="review-gate-reusable", old_ref=OLD,
                            new_ref=TIP, tip_sha=TIP, token="t", dry_run=True),
                ("dry-run", None))


class ProtectedAutomerge(unittest.TestCase):
    def test_effective_required_checks_enable_merge_commit(self):
        calls = []
        def gh(args, *, token):
            calls.append(args)
            if args[1] == 'repos/maxi-tools/x':
                return json.dumps('main')
            if '/rules/branches/' in args[1]:
                return json.dumps([{'type': 'required_status_checks',
                                    'parameters': {'required_status_checks': [{'context': 'build'}]}}])
            if '/protection/' in args[1]:
                raise RuntimeError('HTTP 404')
            return ''
        with mock.patch.object(fp, '_gh', gh):
            fp._enable_automerge(fp.Consumer('maxi-tools/x'), 'https://github.com/maxi-tools/x/pull/9', token='t')
        self.assertEqual(calls[-1], ['pr', 'merge', 'https://github.com/maxi-tools/x/pull/9',
                                     '--repo', 'maxi-tools/x', '--auto', '--merge'])

    def test_empty_rules_do_not_merge(self):
        calls = []
        def gh(args, *, token):
            calls.append(args)
            if args[1] == 'repos/maxi-tools/x':
                return json.dumps('main')
            if '/protection/' in args[1]:
                raise RuntimeError('HTTP 404')
            return '[]'
        with mock.patch.object(fp, '_gh', gh):
            fp._enable_automerge(fp.Consumer('maxi-tools/x'), 'url', token='t')
        self.assertFalse(any(c[:2] == ['pr', 'merge'] for c in calls))

    def test_unreadable_rules_fail_closed(self):
        with mock.patch.object(fp, '_gh', side_effect=['"main"', RuntimeError('HTTP 403')]):
            with self.assertRaises(RuntimeError):
                fp._enable_automerge(fp.Consumer('maxi-tools/x'), 'url', token='t')


class OwnedSyncRouting(unittest.TestCase):
    def test_owned_copy_is_not_a_second_pin_pr(self):
        import base64
        text = '# maxi-config-owned Maxi review gate workflow.\n' + (
            '    uses: maxi-tools/ci/.github/workflows/review-gate-reusable.yml@' + OLD)
        def gh(args, *, token):
            if args[-1] == '.[].name':
                return 'review-gate.yml\n'
            return base64.b64encode(text.encode()).decode()
        with mock.patch.object(fp, '_gh', gh):
            plan = fp._plan(tip_sha=TIP, consumers=['maxi-tools/x'], token='t')
        self.assertEqual(plan[0].outcome, 'owned-sync')

    def test_source_and_installed_copy_advance_together(self):
        text = '# maxi-config-owned Maxi review gate workflow.\n' + (
            '    uses: maxi-tools/ci/.github/workflows/review-gate-reusable.yml@' + OLD + '\n')
        with tempfile.TemporaryDirectory() as root:
            src = pathlib.Path(root) / 'maxi-review/review-gate.yml'
            dst = pathlib.Path(root) / '.github/workflows/review-gate.yml'
            src.parent.mkdir(parents=True)
            dst.parent.mkdir(parents=True)
            src.write_text(text)
            dst.write_text(text)
            with mock.patch.object(fp, '_run', return_value=''), \
                 mock.patch('tempfile.mkdtemp', return_value=root):
                fp._push_pin_branch(fp.Consumer('maxi-tools/maxi-config'),
                                    head_ref=fp.HEAD_REF, new_ref=TIP, token='t')
            self.assertEqual(src.read_text(), dst.read_text())
            self.assertIn(TIP, src.read_text())

    def test_only_bot_owned_single_file_pin_pr_is_retired(self):
        pr = {'number': 12, 'headRefName': fp.HEAD_REF}
        calls = []
        def gh(args, *, token):
            calls.append(args)
            if args[:2] == ['pr', 'view']:
                return json.dumps({'author': {'login': 'app/maxi-tools-auth'},
                                   'headRefName': fp.HEAD_REF,
                                   'files': [{'path': '.github/workflows/review-gate.yml'}]})
            return ''
        with mock.patch.object(fp, '_open_fanout_prs', return_value=[pr]), \
             mock.patch.object(fp, '_gh', gh):
            fp._retire_owned_pin_prs(fp.Consumer('maxi-tools/x'), token='t')
        self.assertEqual([c[1] for c in calls], ['view', 'close'])


if __name__ == "__main__":
    unittest.main()