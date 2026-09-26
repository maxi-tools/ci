#!/usr/bin/env python3
"""A roster label covers the review that arrives under its login.

The selector publishes `reviewer_label` (`coderabbit`). The collector
reads `author{login}` over GraphQL, which returns the actor's bare slug
(`coderabbitai`) -- the table's `reviewer_key` with `[bot]` removed.
Intersecting the two raw strings matches only the reviewers whose label
happens to equal their login, so a clean PR whose only review came from
CodeRabbit reads as unreviewed. (maxi-dist#312, #310, #859.)

`roster_logins` maps the label to the login before the intersection, and
`evaluate` uses that mapping. An asked label with no row fails closed and
names the label, rather than matching nothing.

The collector's jq program parses names and does not compare them -- the
comparison lives in the gate, which is the only place these cases can
fail. The test still extracts that program and asserts it does not grow a
second comparison, so a future edit that starts matching names in jq is
caught here rather than silently diverging from the mapping.

Run directly: `python3 tests/test_roster_label_login.py`.
"""
from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = ROOT / ".github/actions/pr-review-gate/pr_review_gate.py"
COLLECTOR = ROOT / ".github/actions/collect-pr-review-state/action.yml"


def _load_gate():
    spec = importlib.util.spec_from_file_location("pr_review_gate", GATE)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(GATE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jq_program():
    """The jq program the collector actually runs, extracted from action.yml."""
    text = COLLECTOR.read_text()
    marker = "jq -R -r '\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n            ' < roster_desc.txt", start)
    return text[start:end]


gate = _load_gate()

AUTHOR = "maxiboch"
ASKED = ["coderabbit", "maxi-reviewer"]


def _doc(reviewers, asked=ASKED):
    """A payload with one COMMENTED review per reviewer and a roster."""
    return {
        "author": AUTHOR,
        "isDraft": False,
        "headRefName": "wt/some-change",
        "labels": [],
        "threads": [],
        "reviews": [
            {"author": who, "state": "COMMENTED"} for who in reviewers
        ],
        "roster": {"asked": list(asked), "skipped": 0},
    }


class RosterLabelCoversItsLogin(unittest.TestCase):
    """The acceptance cases, judged through evaluate()."""

    def _reviewed_by(self, reviewers, asked=ASKED):
        ok, lines = gate.evaluate(
            _doc(reviewers, asked), only=gate.ONLY_REVIEWER
        )
        return ok, "\n".join(lines)

    def test_coderabbit_label_covers_the_bot_login(self):
        # GraphQL spells the actor without [bot]; REST spells it with.
        # Both are the review the roster asked for.
        for who in ("coderabbitai", "coderabbitai[bot]"):
            ok, text = self._reviewed_by([who])
            self.assertTrue(ok, text)
            self.assertIn(who, text)

    def test_maxi_reviewer_label_covers_its_own_login(self):
        # The one reviewer whose label equals its login. It has to keep
        # matching, bare and bracketed, or the mapping regressed the
        # case that already worked.
        for who in ("maxi-reviewer", "maxi-reviewer[bot]"):
            ok, text = self._reviewed_by([who])
            self.assertTrue(ok, text)
            self.assertIn(who, text)

    def test_an_unasked_login_does_not_cover(self):
        # cubic was not asked. Its review is real and still does not
        # satisfy a roster that asked for coderabbit and maxi-reviewer.
        ok, text = self._reviewed_by(["cubic-dev-ai"])
        self.assertFalse(ok, text)
        self.assertIn("coderabbit", text)
        self.assertIn("maxi-reviewer", text)

    def test_an_unmapped_label_fails_closed_and_names_itself(self):
        # Matching nothing would read as "nobody reviewed", which is the
        # miss this mapping exists to close. The failure names the label.
        with self.assertRaises(gate.Malformed) as caught:
            gate.evaluate(
                _doc(["someone"], asked=["not-a-reviewer"]),
                only=gate.ONLY_REVIEWER,
            )
        self.assertIn("not-a-reviewer", str(caught.exception))

    def test_the_label_covers_its_login_through_the_mapping_directly(self):
        # The table, stated once. A row that drifts from REVIEWER_TABLE
        # fails here before evaluate() can hide it behind a passing case.
        self.assertEqual(
            gate.roster_logins(["coderabbit", "maxi-reviewer"]),
            {"coderabbitai", "maxi-reviewer"},
        )
        self.assertEqual(
            gate.roster_logins(["cubic", "codacy", "copilot"]),
            {"cubic-dev-ai", "codacy-production",
             "copilot-pull-request-reviewer"},
        )

    def test_a_login_spelled_in_asked_still_resolves(self):
        # The roster publishes labels, but a hand-edited status can spell
        # the login. Refusing a name the table itself uses would fail the
        # gate on a roster that named its reviewer correctly.
        self.assertEqual(
            gate.roster_logins(["coderabbitai", "coderabbitai[bot]"]),
            {"coderabbitai"},
        )


class CollectorDoesNotCompareNames(unittest.TestCase):
    """The jq program parses names. It does not decide who reviewed."""

    def test_the_extracted_program_does_not_match_logins(self):
        program = _jq_program()
        # The comparison belongs to the gate. A jq edit that starts
        # matching reviewer logins would be a second copy of the mapping
        # with nothing keeping the two in step.
        for needle in ("coderabbitai", "cubic-dev-ai", "codacy-production",
                       "copilot-pull-request-reviewer", "LABEL_TO_LOGIN"):
            self.assertNotIn(needle, program)
        # And the extraction yielded the program, not a slice of YAML: it
        # still parses the description it is given. Run rather than read,
        # so a program that no longer parses fails here.
        self.assertIn("def parse:", program)
        self.assertIn("malformed review-roster description", program)


if __name__ == "__main__":
    unittest.main()
