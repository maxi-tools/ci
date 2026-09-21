#!/usr/bin/env python3
"""Read the captured output of dedupe-review-threads.py from stdin and print
the dedupe count (or 0) to stdout.

The script prints exactly one JSON report on its success path, as the LAST
top-level object in its combined stdout/stderr. Earlier text may include
`::error::` annotations, a dry-run preamble, or `::warning::` lines that
GitHub itself parses.

We ask the JSON parser where each object ends rather than counting braces.
A depth walk cannot tell a brace inside a string literal from a structural
one, and the text it walks includes BOT COMMENT BODIES -- which in this org
routinely contain `${{ ... }}` workflow expressions, code fences and URLs.
One unbalanced brace in a quoted body shifted every subsequent boundary, so
the last "object" was a slice that did not parse and the action reported
`nothing to dedupe` over a run that had resolved threads. `raw_decode`
knows about string literals; a recursive regex would need Python >=3.14.
(codacy, on maxi-config#797, which vendors this file.)

Lives in the action's directory so the run step can call it as
`"${{ github.action_path }}/parse_summary.py"` without a checkout. The
companion shell script writes this file's stdout into the step summary as
the `deduped N threads` / `nothing to dedupe` line.
"""
from __future__ import annotations

import json
import sys


def find_top_level_objects(text: str) -> list[dict]:
    """Every JSON object in `text`, in order, decoded.

    Scans to each `{` and asks the decoder to read one value there. A
    position that does not start a valid object is skipped -- that is the
    common case for prose containing a brace -- and the scan resumes one
    character later. A position that DOES decode is consumed whole, so a
    brace inside one of its string literals can never be mistaken for a
    boundary.
    """
    decoder = json.JSONDecoder()
    found: list[dict] = []
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict):
            found.append(value)
            index = text.find("{", end)
        else:
            index = text.find("{", index + 1)
    return found


def main() -> int:
    text = sys.stdin.read()
    objects = find_top_level_objects(text)
    if not objects:
        # No JSON report in the captured output -- the script claimed
        # success but printed nothing parseable. Treat as zero rather than
        # crashing, because the wrapper shell decides the summary line and
        # we want a deterministic "nothing to dedupe" over a script bug
        # surfacing as a parse error here. The wrapper still sees a 0 and
        # writes "nothing to dedupe", which is at least an honest line.
        print(0)
        return 0
    report = objects[-1]
    resolved = report.get("resolved", [])
    # A report whose `resolved` is not a list is a script bug, not a count.
    # Print 0 rather than raising: the wrapper turns this number into the
    # summary line, and "nothing to dedupe" is an honest line where a
    # traceback in a parser would only hide the real failure upstream.
    print(len(resolved) if isinstance(resolved, list) else 0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
