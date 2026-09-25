#!/usr/bin/env python3
"""Both roster parsers read the previous wire format and the current one.

maxi-config#854 shortened the `review-roster` status to fit GitHub's
140-character cap. The parsers that landed with it accept only the new
line, and every head not re-reviewed since still carries the previous
one:

    band=<band> asked=[<names>] skipped=[<names>] profiles=<state>

The under-cap, valid old status

    band=trivial asked=[maxi-reviewer,coderabbit] skipped=[claude-review] profiles=present

(86 characters) parsed with the old production jq program and raises
`malformed review-roster description` on the new one. The gate then fails
closed on that head, in every repo that consumes maxi-tools/ci@main.

Both parsers -- Python `parse_roster` and the jq program embedded in
collect-pr-review-state/action.yml -- now accept that line and normalise
`skipped` to a count. These tests run the REAL extracted jq program, not
a regex approximation of it, beside the new-format round trip.

Run directly: `python3 tests/test_roster_parse.py`.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = ROOT / ".github/actions/pr-review-gate/pr_review_gate.py"
COLLECTOR = ROOT / ".github/actions/collect-pr-review-state/action.yml"

# The producer output the verifier reproduced against. 86 characters, one
# skipped name. Under the cap, and still published on heads not re-reviewed
# since the shortening.
LEGACY_ONE = (
    "band=trivial asked=[maxi-reviewer,coderabbit] "
    "skipped=[claude-review] profiles=present"
)
# Same shape, two skipped names. The count is the length of the list.
LEGACY_TWO = (
    "band=routine asked=[maxi-reviewer,coderabbit] "
    "skipped=[claude-review,cubic] profiles=present"
)
# What the selector publishes now. Same asked set, skipped already a count.
CURRENT = "asked=[maxi-reviewer,coderabbit] skipped=1"
CURRENT_UNKNOWN = "asked=[maxi-reviewer,coderabbit] skipped=1 unknown=2"

EXPECTED_ONE = {
    "asked": ["maxi-reviewer", "coderabbit"],
    "skipped": 1,
}
EXPECTED_TWO = {
    "asked": ["maxi-reviewer", "coderabbit"],
    "skipped": 2,
}


def _load_gate():
    spec = importlib.util.spec_from_file_location("pr_review_gate", GATE)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(GATE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jq_program():
    """The jq program the collector actually runs, extracted from action.yml.

    The program is a single-quoted `jq -R -r '...'` block. Reading it back
    out of the file, rather than restating it, is the point: a hand-copied
    approximation is what let the previous compat test pass while the
    production program rejected the old producer output.
    """
    text = COLLECTOR.read_text()
    marker = "jq -R -r '\n"
    start = text.index(marker) + len(marker)
    end = text.index("\n            ' < roster_desc.txt", start)
    # action.yml indents the program; jq does not care, but the quotes are
    # YAML-escaped (`\\` is one backslash to jq) and have to be unescaped
    # the way the shell unescapes a single-quoted string -- which is to
    # say, not at all. The file stores the backslashes jq wants.
    return text[start:end]


def _run_jq(description):
    """Run the extracted program the way the collector does: raw input, -r.

    The collector writes the description to a file and redirects it; jq -R
    reads one raw line either way, so the program sees the same input.
    """
    program = _jq_program()
    return subprocess.run(
        ["jq", "-R", "-r", program],
        input=description,
        capture_output=True,
        text=True,
        timeout=30,
    )


gate = _load_gate()


class LegacyProducerRoundTrip(unittest.TestCase):
    """Old producer output and new producer output parse to the same roster."""

    def test_the_reproduced_86_character_status_parses(self):
        # Pinned length: the verifier's reproduction is this exact string,
        # and a rewrite that "fixes" a different string does not fix it.
        self.assertEqual(len(LEGACY_ONE), 86)
        self.assertEqual(gate.parse_roster(LEGACY_ONE), EXPECTED_ONE)

    def test_a_multi_name_skip_list_counts_as_its_length(self):
        self.assertEqual(gate.parse_roster(LEGACY_TWO), EXPECTED_TWO)

    def test_the_new_format_parses_to_the_same_roster(self):
        self.assertEqual(gate.parse_roster(CURRENT), EXPECTED_ONE)
        self.assertEqual(gate.parse_roster(CURRENT_UNKNOWN), EXPECTED_ONE)

    def test_an_empty_legacy_skip_list_counts_as_zero(self):
        # Both shapes. The brackets have to come off before the split, or
        # `skipped=[]` counts as one name.
        self.assertEqual(
            gate.parse_roster("asked=[maxi-reviewer] skipped=[]"),
            {"asked": ["maxi-reviewer"], "skipped": 0},
        )
        self.assertEqual(
            gate.parse_roster(
                "band=trivial asked=[maxi-reviewer] skipped=[] profiles=present"
            ),
            {"asked": ["maxi-reviewer"], "skipped": 0},
        )

    def test_a_trailing_newline_still_parses(self):
        # The collector reads the description with `printf '%s'`, which
        # drops a trailing newline one way and can preserve it another.
        self.assertEqual(gate.parse_roster(LEGACY_ONE + "\n"), EXPECTED_ONE)
        self.assertEqual(gate.parse_roster(CURRENT + "\n"), EXPECTED_ONE)

    def test_a_band_prefix_that_does_not_parse_fails_closed(self):
        # A description that STARTS like a roster and is not one is the
        # dangerous shape: ignoring it would turn a corrupt status into a
        # green gate. Both prefixes fail closed.
        for desc in (
            "band=trivial asked=[a,b",
            "band=trivial skipped=[c]",
            "band=",
            "asked=[a,b",
            "asked=[a]",
        ):
            with self.assertRaises(gate.Malformed, msg=repr(desc)):
                gate.parse_roster(desc)

    def test_an_unrelated_description_is_not_a_roster(self):
        for desc in ("", "   ", "reviewed by a non-author", None):
            self.assertIsNone(gate.parse_roster(desc), repr(desc))


class ExtractedJqProgram(unittest.TestCase):
    """The jq program in action.yml, extracted and run, not approximated."""

    def test_the_extracted_program_is_the_production_one(self):
        program = _jq_program()
        # Both arms, by the error string only the production program raises
        # and by the two prefixes it matches. A copy that dropped an arm
        # fails here before the cases below can pass by accident.
        self.assertIn("malformed review-roster description", program)
        self.assertIn("band=[a-z]+", program)
        self.assertIn("asked=", program)
        # And it is a jq program, not a comment about one.
        self.assertIn("def parse:", program)

    def _parsed(self, description):
        proc = _run_jq(description)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_legacy_and_current_parse_to_the_same_roster(self):
        self.assertEqual(self._parsed(LEGACY_ONE), EXPECTED_ONE)
        self.assertEqual(self._parsed(LEGACY_TWO), EXPECTED_TWO)
        self.assertEqual(self._parsed(CURRENT), EXPECTED_ONE)
        self.assertEqual(self._parsed(CURRENT_UNKNOWN), EXPECTED_ONE)

    def test_an_empty_skip_list_counts_as_zero(self):
        self.assertEqual(
            self._parsed("asked=[maxi-reviewer] skipped=[]"),
            {"asked": ["maxi-reviewer"], "skipped": 0},
        )
        self.assertEqual(
            self._parsed(
                "band=trivial asked=[maxi-reviewer] skipped=[] profiles=present"
            ),
            {"asked": ["maxi-reviewer"], "skipped": 0},
        )

    def test_a_description_of_neither_shape_fails_closed(self):
        proc = _run_jq("reviewed by a non-author")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("malformed review-roster description", proc.stderr)

    def test_python_and_jq_agree(self):
        # The two parsers are copies. A description one accepts and the
        # other rejects is the defect this file exists to catch.
        for desc in (LEGACY_ONE, LEGACY_TWO, CURRENT, CURRENT_UNKNOWN,
                     "asked=[maxi-reviewer] skipped=[]",
                     "band=trivial asked=[maxi-reviewer] skipped=[] profiles=present"):
            self.assertEqual(self._parsed(desc), gate.parse_roster(desc), desc)


if __name__ == "__main__":
    unittest.main()
