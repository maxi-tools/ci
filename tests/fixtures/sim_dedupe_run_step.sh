#!/usr/bin/env bash
# Test fixture: a stripped, controlled copy of the run step in
# .github/actions/dedupe-pr-review-threads/action.yml.
#
# Drives a fake python script at $1 with two arguments (the script path
# and the path to the GITHUB_STEP_SUMMARY file the run step writes),
# and mirrors the three-branch shape the real action.yml uses:
#
#   rc == 0  +  resolved count > 0   ->  "deduped N threads"
#   rc == 0  +  resolved count == 0  ->  "nothing to dedupe"
#   rc != 0                          ->  "dedupe unavailable: <reason>"
#
# Kept in sync with the real wrapper by
# `tests/test_dedupe_review_threads.py::ActionSummaryLines::
# test_the_action_yml_run_step_matches_the_shim` -- a future edit to
# either side that changes the three-branch shape or the literal
# summary lines fails that test.
#
# Args:
#   $1  path to a python script that emits (to stderr) whatever the test
#       wants the run step to capture, then exits with the test's chosen
#       code.
#   $2  path to a file that will receive the summary output.
set -uo pipefail

SCRIPT="$1"
SUMMARY="$2"

out="$(mktemp)"
trap 'rm -f "$out"' EXIT
python3 "$SCRIPT" >"$out" 2>&1
rc=$?
cat "$out" >>"$SUMMARY"
if [ "$rc" -ne 0 ]; then
  reason="$(tail -n 20 "$out" | awk 'NF{f=$0} END{print f}')"
  if [ -z "$reason" ]; then
    reason="the dedupe script exited with code $rc and produced no explanation"
  fi
  printf 'dedupe unavailable: %s\n' "$reason" | tee -a "$SUMMARY"
  exit "$rc"
fi
deduped="$(python3 -c '
import sys, json, re
text = sys.stdin.read()
depth = 0
start = None
matches = []
for i, ch in enumerate(text):
    if ch == "{":
        if depth == 0:
            start = i
        depth += 1
    elif ch == "}":
        depth -= 1
        if depth == 0 and start is not None:
            matches.append(text[start:i + 1])
            start = None
if not matches:
    print(0)
    sys.exit(0)
try:
    print(len(json.loads(matches[-1]).get("resolved", [])))
except Exception:
    print(0)
' <"$out")"
if [ "${deduped:-0}" -eq 0 ]; then
  printf 'nothing to dedupe\n' | tee -a "$SUMMARY"
else
  printf 'deduped %s threads\n' "$deduped" | tee -a "$SUMMARY"
fi
