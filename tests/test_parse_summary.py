#!/usr/bin/env python3
"""parse_summary.py turns the dedupe script's output into the summary count.

It had no tests. It is also the step that decides what the PR page says
about a dedupe run, so when it reads wrong the run says `nothing to
dedupe` over work it actually did -- a failed measurement reported as a
result, which is the defect class this repository keeps finding.

The specific way it read wrong: it located JSON by counting braces, and
the text it scans is the script's own combined output, which quotes BOT
COMMENT BODIES. Those routinely carry `${{ ... }}`, code fences and URLs
in this org. One unbalanced brace inside a quoted body shifted every
boundary after it. (codacy, on maxi-config#797.)

Run directly: `python3 tests/test_parse_summary.py`.
"""
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import unittest

ACTION = (pathlib.Path(__file__).resolve().parents[1]
          / ".github/actions/dedupe-pr-review-threads/parse_summary.py")

spec = importlib.util.spec_from_file_location("parse_summary", ACTION)
ps = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ps
spec.loader.exec_module(ps)


def count_for(text: str) -> str:
    """Run the script exactly as action.yml does: stdin in, count out."""
    proc = subprocess.run([sys.executable, str(ACTION)], input=text,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


REPORT = {"resolved": ["T1", "T2", "T3"], "kept": ["T0"]}


class TheCountIsRead(unittest.TestCase):
    def test_a_plain_report_counts_its_resolved_threads(self):
        self.assertEqual(count_for(json.dumps(REPORT)), "3")

    def test_the_last_report_wins(self):
        text = json.dumps({"resolved": []}) + "\n" + json.dumps(REPORT)
        self.assertEqual(count_for(text), "3")

    def test_annotations_before_the_report_are_ignored(self):
        text = ("::warning::rate limited, retrying\n"
                "dry-run: would resolve 3 threads\n"
                + json.dumps(REPORT))
        self.assertEqual(count_for(text), "3")


class BracesInsideStringsDoNotMoveTheBoundary(unittest.TestCase):
    """THE defect. Each body below breaks a brace-counting scan."""

    def _with_body(self, body: str) -> str:
        return ("::warning::echoing the bot comment below\n"
                + json.dumps({"skipped": [{"body": body}]}) + "\n"
                + json.dumps(REPORT))

    def test_a_workflow_expression_in_a_quoted_body(self):
        # An unmatched `{{` is what a review comment quoting a workflow
        # snippet looks like, and this repository is full of them.
        self.assertEqual(count_for(self._with_body("use ${{ github.ref }")), "3")

    def test_an_unmatched_closing_brace(self):
        self.assertEqual(count_for(self._with_body("stray } in prose")), "3")

    def test_an_unmatched_opening_brace(self):
        self.assertEqual(count_for(self._with_body("stray { in prose")), "3")

    def test_a_brace_heavy_code_fence(self):
        self.assertEqual(
            count_for(self._with_body("```\nfn f() { g({ h: 1 }) }\n```")), "3")

    def test_the_old_brace_walk_would_have_failed_these(self):
        """Non-vacuity: the cases above must actually defeat brace counting,
        or they are testing nothing."""
        def brace_walk(text):
            depth, start, out = 0, None, []
            for i, ch in enumerate(text):
                if ch == "{":
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and start is not None:
                        out.append(text[start:i + 1])
                        start = None
            return out

        text = self._with_body("use ${{ github.ref }")
        old = brace_walk(text)
        self.assertTrue(
            not old or not self._parses(old[-1]),
            "the brace walk handled this input, so the test proves nothing")
        self.assertEqual(count_for(text), "3")

    @staticmethod
    def _parses(chunk: str) -> bool:
        try:
            json.loads(chunk)
            return True
        except ValueError:
            return False


class MalformedOutputIsZeroNotACrash(unittest.TestCase):
    def test_no_json_at_all(self):
        self.assertEqual(count_for("::error::everything went wrong\n"), "0")

    def test_empty_input(self):
        self.assertEqual(count_for(""), "0")

    def test_a_report_without_resolved(self):
        self.assertEqual(count_for(json.dumps({"kept": ["T0"]})), "0")

    def test_resolved_that_is_not_a_list(self):
        self.assertEqual(count_for(json.dumps({"resolved": "three"})), "0")

    def test_a_json_array_is_not_mistaken_for_the_report(self):
        text = json.dumps([1, 2, 3]) + "\n" + json.dumps(REPORT)
        self.assertEqual(count_for(text), "3")


if __name__ == "__main__":
    unittest.main()
