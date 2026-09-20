#!/usr/bin/env python3
"""Read the captured output of dedupe-review-threads.py from stdin and print
the dedupe count (or 0) to stdout.

The script prints exactly one JSON report on its success path, as the LAST
top-level object in its combined stdout/stderr. Earlier text may include
`::error::` annotations, a dry-run preamble, or `::warning::` lines that
GitHub itself parses.

We track brace depth rather than using a recursive regex so the action does
not depend on Python >=3.14 (the `re` module's `(?N)` recursive subpattern
was added in 3.14). A depth walk is O(N) in the captured text size, and the
captured text is the script's own output which is bounded by the PR's
thread count.

Lives in the action's directory so the run step can call it as
`"${{ github.action_path }}/parse_summary.py"` without a checkout. The
companion shell script writes this file's stdout into the step summary as
the `deduped N threads` / `nothing to dedupe` line.
"""
from __future__ import annotations

import json
import sys


def find_top_level_objects(text: str) -> list[str]:
    depth = 0
    start: int | None = None
    matches: list[str] = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                matches.append(text[start : i + 1])
                start = None
    return matches


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
    try:
        report = json.loads(objects[-1])
    except json.JSONDecodeError:
        print(0)
        return 0
    print(len(report.get("resolved", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
