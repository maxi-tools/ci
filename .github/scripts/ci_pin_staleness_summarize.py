#!/usr/bin/env python3
'''Turn the fanout_ci_pin.py JSON plan into a staleness summary.

Reads JSON Lines from stdin (one object per consumer/workflow pair):

    {"consumer": "maxi-tools/X", "workflow_file": "...", "old_ref": "...",
     "new_ref": "..."}

Writes to stdout a structured summary:

    LAGGARD maxiboch/maxi-core review-gate-reusable 44793c2 -> 0e5111c4 (3 behind)
    OK      maxi-tools/maxi-X ...

where "behind" is the count of distinct ci merge-commit SHAs strictly
between the consumer's pinned sha and the tip. The count is read from
the local ci repo (`git rev-list --count <old>..<tip>`); this script
must be run from inside the ci checkout (the workflow guarantees that).

Threshold: more than ONE sha behind -> LAGGARD. The fan-out PR is the
primary detector for the FIRST sha; this script flags consumers that
have ignored the fan-out PR for at least one more commit.
'''

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _behind_count(old: str, new: str) -> int:
    proc = subprocess.run(
        ['git', 'rev-list', '--count', f'{old}..{new}'],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    if proc.returncode != 0:
        return -1
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return -1


def main() -> int:
    if not sys.stdin.isatty():
        # Reading from a pipe (the workflow case).
        data = sys.stdin.read()
    else:
        data = ''

    laggards: list[str] = []
    oks: list[str] = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        old = rec['old_ref']
        new = rec['new_ref']
        if old == new:
            oks.append(f'{rec["consumer"]} {rec["workflow_file"]} at tip')
            continue
        behind = _behind_count(old, new)
        if behind < 0:
            oks.append(
                f'{rec["consumer"]} {rec["workflow_file"]} '
                f'{old[:12]} (uncommitted ancestry)'
            )
            continue
        short_old = old[:12]
        short_new = new[:12]
        line_out = (
            f'{rec["consumer"]} {rec["workflow_file"]} '
            f'{short_old} -> {short_new} ({behind} behind)'
        )
        if behind > 1:
            laggards.append(line_out)
        else:
            oks.append(line_out)

    # Header line so the issue-body templater can find the table
    # without parsing free-form prose.
    for lag in laggards:
        print(f'LAGGARD {lag}')
    for ok in oks:
        print(f'OK      {ok}')
    return 0


if __name__ == '__main__':
    sys.exit(main())