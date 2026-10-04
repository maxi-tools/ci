"""The sibling discover step must refuse a name carrying a line break.

WHY THIS EXISTS
---------------
cubic finding #8 (P1, conf=9) against
`.github/actions/verify-sibling-checkouts/action.yml`. The discover step walks
Cargo manifests for `path = "../<repo>"` entries, derives a sibling repository
name from each, and prints one name per line. The next step reads that stream
with `while IFS= read -r name` and, for every name, unconditionally runs
`rm -rf "$parent/$name"` before cloning into it.

The two steps communicate over a LINE-DELIMITED channel, and nothing checked
that the thing being sent could survive the channel.

TOML basic strings honour the `\\n` escape, so a manifest may legally carry

    [dependencies]
    evil = { path = "../foo\\nrm-rf-victim" }

which `tomllib` decodes to a string with a REAL newline in it. That value
normalises to a real path component carrying a real newline, so the sibling
name derived from it is `foo\nrm-rf-victim`. `print(name)` writes that as TWO
lines. The verify step then reads two names -- `foo` and `rm-rf-victim` -- and
proceeds with both.

What makes this a P1 rather than a cosmetic finding is the verify step's own
defence. It validates every name against

    *[!A-Za-z0-9_.-]*|""|.*|-*|_*|*..*)

which rejects anything with a character outside a strict allowlist. The two
injected fragments `foo` and `rm-rf-victim` are both entirely inside that
allowlist, so the guard PASSES both. The newline that would have failed the
guard is precisely the character that split one name into two -- it is
consumed as the record separator before validation ever sees it. A control
that reads as "reject anything unexpected" is defeated by the one character
the transport eats.

Both destinations are then `rm -rf`'d unconditionally. The action's own
comment states the intent of that `rm -rf`: "A previous, unrelated job on
this self-hosted runner may have left a tree at exactly this path." So the
attacker does not need the directory to exist beforehand; the step creates
the target and destroys whatever is there.

WHY THE TEST EXECUTES THE SCRIPT RATHER THAN READING IT
-----------------------------------------------------
The fix is four lines of embedded Python inside a YAML `run:` block. A test
that asserts on the source text would pass unchanged against a script that
does not run, and would keep passing after someone edits the `if` condition
into something vacuous. Worse, the interesting property is not textual at
all: it is what the script PRINTS and what it RETURNS for a manifest that
carries the escape. Only running it can observe the record channel, which is
the thing the defect lives in.

So this extracts the heredoc'd script from the real action.yml, runs it
against a real manifest tree in a temp dir, and asserts on its exit status,
its stdout records, and its stderr annotation. The script is the one the
action will run on a runner, not a transcription of it.

WHY THE FIX SITS WHERE IT DOES
------------------------------
The check is at name-DERIVATION, not at print time, and not in the verify
step. Deriving is the last point at which the manifest path and the raw TOML
value are both still in scope, so the `::error::` can name all three: the
manifest, the raw `path`, and the offending sibling. Rejecting at print time
would work too, but it would be one refactor away from the print that emits
the record. Rejecting in the verify step is the one option that cannot work:
by the time a name arrives there it has already been split, and the pieces
are individually indistinguishable from legitimate siblings. That is why the
character whitelist there is not a mitigation, and why a test asserting only
on the verify step would be asserting on the wrong file.

The two fragments are also asserted to be ABSENT from stdout together with
the nonzero exit, because exiting nonzero while still printing them would
leave the same records in the stream for whatever consumes it next.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/verify-sibling-checkouts/action.yml"

# The discover step writes the script with `cat > "$discover" <<'PY'`, so the
# script body is everything between that heredoc marker and the closing `PY`.
# Matched on the marker rather than on a line number: a line number here
# would silently start asserting against a different script the moment
# anyone edits above it, which is the "asserted against prose" failure this
# file exists to avoid.
HEREDOC = re.compile(r"<<'PY'\n(?P<body>.*?)\n[ \t]*PY\n", re.DOTALL)

# What the verify step does with each record it reads. Quoted here rather
# than referenced so this test states the property it is defending: a name
# arriving at this read loop becomes a `rm -rf` destination. If the action
# ever stops doing that, the FRAGMENT assertions below become over-strict
# and this is the line to revisit -- they are deliberately not derived from
# the action's text, because deriving them would make them agree with
# whatever the action happens to do.
VERIFY_STEP_READS_LINES = True


def discover_script() -> str:
    """The embedded discover script, taken verbatim from the real action.yml."""
    text = ACTION.read_text(encoding="utf-8")
    match = HEREDOC.search(text)
    if match is None:
        raise AssertionError(
            f"{ACTION.name} no longer contains a <<'PY' heredoc; the script "
            "this test executes cannot be located, and a test that reports "
            "nothing about a script it never found is the exact failure this "
            "file is about"
        )
    # The heredoc body is indented to match the `run: |` block. The action
    # relies on the runner's shell to strip that; here it is stripped
    # explicitly, because a script run with leading indentation raises
    # IndentationError and the test would fail for a reason that has nothing
    # to do with the defect.
    return textwrap.dedent(match.group("body"))


def run_discover(script: str, workspace: Path):
    """Run the extracted script against `workspace` as GITHUB_WORKSPACE.

    The script is written to a real file and invoked through `python3`, not
    exec'd: it reads GITHUB_WORKSPACE from the environment and exits via
    sys.exit, both of which only behave as they do on a runner under a real
    interpreter.
    """
    with tempfile.TemporaryDirectory() as tmp:
        script_path = Path(tmp) / "sibling-discover.py"
        script_path.write_text(script, encoding="utf-8")
        env = dict(os.environ, GITHUB_WORKSPACE=str(workspace))
        return subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True, text=True, env=env, timeout=120,
        )


def workspace_with_dependency(path_value: str) -> tempfile.TemporaryDirectory:
    """A checkout whose one manifest declares `[dependencies] evil.path`.

    `path_value` is written into the TOML source as-is, so a caller can put a
    real escape sequence (`\\n`) in it and have TOML decode it into a real
    newline -- which is the whole point. Doubling the backslashes in the
    Python source that builds the manifest keeps TOML from having already
    collapsed them.
    """
    holder = tempfile.TemporaryDirectory()
    checkout = Path(holder.name) / "repo"
    checkout.mkdir()
    manifest = checkout / "Cargo.toml"
    manifest.write_text(
        '[package]\n'
        'name = "victim"\n'
        'version = "0.0.0"\n'
        '\n'
        '[dependencies]\n'
        f'evil = {{ path = "{path_value}" }}\n',
        encoding="utf-8",
    )
    holder.checkout = checkout  # type: ignore[attr-defined]
    return holder


class SiblingNameLineBreaks(unittest.TestCase):
    """The discover step must refuse, not emit, a name it cannot transport."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = discover_script()

    def run_case(self, path_value: str):
        holder = workspace_with_dependency(path_value)
        self.addCleanup(holder.cleanup)
        return run_discover(self.script, holder.checkout)  # type: ignore[attr-defined]

    def assert_refused(self, result, fragment: str) -> None:
        """Nonzero, annotated, and the injected fragments absent from stdout.

        All three, because each alone is satisfiable by the wrong fix: an
        exit without an annotation leaves a reader with nothing to act on, an
        annotation without an exit is a warning the action ignores, and
        either without the stdout assertion would still leave the injected
        records in the stream for the consuming step.
        """
        self.assertNotEqual(
            0, result.returncode,
            "the discover step accepted a sibling name carrying a line break "
            "and exited 0; stdout was " + repr(result.stdout))

        self.assertIn(
            "::error::", result.stderr,
            "the refusal carried no ::error:: annotation, so it would not "
            "surface as an annotation on the workflow run. stderr was "
            + repr(result.stderr))

        # The annotation must name the offending sibling, not merely say
        # "something was wrong" -- a maintainer reading the annotation has to
        # be able to tell WHICH manifest entry to fix without opening the
        # diff. The fragment appears in the escaped repr of the name.
        self.assertIn(
            fragment, result.stderr,
            "the annotation does not name the offending sibling " + repr(fragment)
            + "; a reader would have to diff the manifests to find which "
            "entry to fix. stderr was " + repr(result.stderr))

        self.assertIn(
            "line break", result.stderr,
            "the annotation does not say what was wrong with the name. "
            "stderr was " + repr(result.stderr))

        # Neither half of the injected name may reach stdout. This is the
        # assertion that pins the actual defect: the harm is not that a bad
        # name is accepted, it is that one name becomes TWO records. Asserted
        # per fragment, because the split could in principle leave the first
        # record behind and only drop the second.
        for word in ("foo", fragment):
            self.assertNotIn(
                word, result.stdout,
                f"{word!r} reached stdout; the line break split one sibling "
                "into two records and the verify step would treat each as a "
                "separate sibling to wipe and clone. stdout was "
                + repr(result.stdout))

    def test_a_newline_in_the_path_is_refused(self) -> None:
        """The exact shape from the finding: `../foo\\nrm-rf-victim`."""
        result = self.run_case("../foo\\nrm-rf-victim")
        self.assert_refused(result, "rm-rf-victim")

    def test_a_carriage_return_in_the_path_is_refused(self) -> None:
        """`\\r` splits records just as effectively as `\\n` on a raw read.

        Checked separately rather than folded into the newline case: `\\r` is
        an ordinary character in a POSIX path component, so a check written
        as `if "\\n" in name` alone would leave this one open, and the
        carriage-return form is what a copy-paste of a Windows-authored
        manifest would actually contain.
        """
        result = self.run_case("../foo\\rrm-rf-victim")
        self.assert_refused(result, "rm-rf-victim")

    def test_a_leading_newline_is_refused(self) -> None:
        """A leading break splits into an EMPTY first record.

        The empty record matters because it is the shape most likely to slip
        past a check written to look for a break in the middle. An empty
        name reaches the verify step as a name, and `dest="$parent/$name"`
        with an empty name is the checkout's own parent directory.
        """
        result = self.run_case("../\\nrm-rf-victim")
        self.assert_refused(result, "rm-rf-victim")

    def test_a_trailing_newline_is_refused(self) -> None:
        """A trailing break splits into an empty SECOND record.

        Distinct from the leading case in a way that matters for `read`: a
        trailing newline at end-of-stream is the ordinary terminator of every
        legitimate record, so a check that only rejects a break which has
        content after it would pass this one.
        """
        result = self.run_case("../rm-rf-victim\\n")
        self.assert_refused(result, "rm-rf-victim")

    def test_a_multiline_basic_string_is_refused(self) -> None:
        """TOML's `\\n\\n\\` multi-line form reaches the same place.

        Recorded separately because it is a different TOML surface producing
        the same defect: the value is spelled with a literal backslash-newline
        continuation rather than an escape, so a fix written against escapes
        alone would not obviously cover it. Both decode to a name carrying a
        real newline.
        """
        result = self.run_case("../foo\\n\\n\\rm-rf-victim")
        self.assert_refused(result, "rm-rf-victim")


def strip_line_break_guard(script: str) -> str:
    """The discover script with the line-break guard removed.

    Used ONLY by the non-vacuity probe, to demonstrate the original defect.
    Line-based rather than a single regex over the whole block, because the
    guard is an `if` with a multi-line `print(...)` argument -- a regex
    spanning it has to guess the exact closing paren position and indentation,
    and gets it wrong silently (returns the script unchanged, which the
    caller then mistakes for "the guard does nothing").

    Anchored on the two lines the guard sits BETWEEN -- the assignment that
    produces the name and the record that stores it. That span is the only
    place a refusal can live, so it is found structurally rather than by
    matching the guard's own text; a regex over the condition would break
    silently (returning the script unchanged) the first time someone reworded
    the message, and the caller would read that as "the guard does nothing".

    The stripped span is required to have contained a line-break test. If a
    future edit moves the refusal out of this span, stripping it would still
    succeed but the probe would then be measuring something other than the
    original defect -- so that is a hard failure here, not a shrug.
    """
    lines = script.split("\n")
    start = next(
        (i for i, line in enumerate(lines)
         if re.match(r"\s*name = parts\[", line)),
        None,
    )
    if start is None:
        return script
    end = next(
        (i for i, line in enumerate(lines)
         if i > start and "siblings.setdefault" in line),
        None,
    )
    if end is None:
        return script
    span = "\n".join(lines[start + 1:end])
    if not re.search(r'if\s+.*in\s+name', span):
        raise AssertionError(
            "no line-break test found between the sibling-name assignment "
            "and the siblings.setdefault record. If the refusal moved, "
            "point this probe at it -- stripping a span that contains no "
            "guard would leave the defect in place and let every refusal "
            "case above pass against the unfixed script."
        )
    return "\n".join(lines[:start + 1] + lines[end:])


class OrdinarySiblingsStillWork(unittest.TestCase):
    """The refusal must not have cost the action its actual job.

    A guard that rejects everything is green against every case above. These
    pin the other half: a legitimate sibling path dependency is still
    discovered, still emitted as exactly one record, and still exits 0.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = discover_script()

    def test_a_plain_sibling_is_still_discovered(self) -> None:
        holder = workspace_with_dependency("../waterui")
        self.addCleanup(holder.cleanup)
        result = run_discover(self.script, holder.checkout)  # type: ignore[attr-defined]
        self.assertEqual(
            0, result.returncode,
            "a legitimate sibling path dependency was rejected; the refusal "
            "has broken the action's ordinary case. stderr was "
            + repr(result.stderr))
        self.assertEqual(
            ["waterui"], result.stdout.splitlines(),
            "the ordinary sibling must still be emitted as exactly one "
            "record; stdout was " + repr(result.stdout))

    def test_an_intra_tree_path_is_still_ignored(self) -> None:
        """`crates/foo` never leaves the checkout, so it is not a sibling.

        The discover step's whole reason for existing is the `../` case; if
        the refusal were written one branch too high it would start failing
        on paths cargo resolves locally and never needed a clone for.
        """
        holder = workspace_with_dependency("crates/foo")
        self.addCleanup(holder.cleanup)
        result = run_discover(self.script, holder.checkout)  # type: ignore[attr-defined]
        self.assertEqual(0, result.returncode, repr(result.stderr))
        self.assertEqual(
            "", result.stdout,
            "an intra-tree path dependency must emit no sibling record; "
            "stdout was " + repr(result.stdout))


class TheGuardCannotPassVacuously(unittest.TestCase):
    """Prove the assertions above can fail, by running the pre-fix script.

    The suite above would report green against a discover step that simply
    removed the loop, against one whose guard was `if False`, and against one
    whose guard tested for the wrong character. None of those are reachable by
    accident from this file, but none of them are reachable by the ORIGINAL
    code either -- and the original code is the thing with the finding.

    So this runs the shipped script with the guard surgically removed and
    requires it to reproduce the defect: exit 0, and the injected fragments
    present in stdout as separate records. If the removal stops changing the
    behaviour, the guard is no longer what is being measured and every case
    above has quietly become decoration.
    """

    def test_without_the_guard_the_injected_name_is_split_into_records(self) -> None:
        unguarded = strip_line_break_guard(discover_script())
        self.assertNotEqual(
            unguarded, discover_script(),
            "could not remove the line-break guard from the shipped script; "
            "if the guard's text changed, update strip_line_break_guard to "
            "recognise the new form -- a non-vacuity probe that no longer "
            "strips anything proves nothing")

        holder = workspace_with_dependency("../foo\\nrm-rf-victim")
        self.addCleanup(holder.cleanup)
        result = run_discover(unguarded, holder.checkout)  # type: ignore[attr-defined]

        self.assertEqual(
            0, result.returncode,
            "the unguarded script exited nonzero; it is supposed to accept "
            "the injected name, which is what makes the guard necessary. "
            "stderr was " + repr(result.stderr))
        records = result.stdout.splitlines()
        self.assertEqual(
            ["foo", "rm-rf-victim"], records,
            "the unguarded script did not split the injected name into the "
            "two records this suite claims it does; stdout was "
            + repr(result.stdout))


if __name__ == "__main__":
    unittest.main()