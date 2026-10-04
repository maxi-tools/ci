#!/usr/bin/env python3
"""Tests for the merge-gate aggregate's queue-starvation classification.

The aggregate step in `.github/workflows/rust-ci.yml` is the SOLE required
status check under the post-merge-gate ruleset, and it is a heredoc'd Python
program embedded in YAML -- so nothing else in the repo executes it, and a
change to it is otherwise only checked by a fleet outage.

These tests EXTRACT that program from the workflow file and run it against a
fake `gh`, over the real API payload shapes. Extracting rather than
duplicating is the point: a copy would let the two drift, and a drifted gate
is the failure this whole change exists to prevent.

Fixtures are shaped from the X64 pool saturation measured on 2026-10-04 (17
runners, 15 busy; `merge-gate / plan / lane-plan` queued up to 2100s and
ending `cancelled` without ever being scheduled), and from the two shapes that
must NOT be relabelled as capacity pressure: a concurrency-group cancellation
and a genuine test failure.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "rust-ci.yml"

# A fake `gh` on PATH, rather than a mock of `subprocess.run`. The aggregate
# program runs in a CHILD interpreter, so patching `subprocess.run` in this
# process does not reach it -- the child would call the real `gh` and the test
# would depend on network state. A real executable on PATH is the only mock the
# child can see.
FAKE_GH = '''#!/usr/bin/env python3
import json, os, sys

if os.environ.get("FAKE_GH_FAIL"):
    sys.stderr.write("HTTP 403: Resource not accessible\\n")
    sys.exit(1)

payload = json.loads(os.environ["FAKE_GH_JOBS"])
sys.stdout.write(json.dumps({"jobs": payload}))
'''


def aggregate_source():
    """The Python the `Aggregate lane results` step actually runs.

    Pulled out of the `run: |` block between the `<<'PY'` heredoc marker and
    its `PY` terminator, then dedented, because a real run gets it dedented by
    YAML block-scalar handling. Sourcing it from the file rather than pasting it
    here means a test failure here is a failure of the workflow, not of a
    stale copy of it.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("- name: Aggregate lane results")
    block = text[start:]
    m = re.search(r"python3 <<'PY'\n(.*?)\n\s*PY\n", block, re.DOTALL)
    if not m:
        raise AssertionError("no `python3 <<'PY'` heredoc in the aggregate step")
    return textwrap.dedent(m.group(1))


def job(name, conclusion="success", steps=3, runner="maxicoconut-3",
        started="2026-10-04T21:00:00Z", job_id=1):
    """One element of `.../runs/{id}/attempts/{n}/jobs`."""
    return {
        "id": job_id,
        "name": name,
        "conclusion": conclusion,
        "steps": [{"name": f"step{i}"} for i in range(steps)],
        "runner_name": runner,
        "started_at": started,
        "completed_at": "2026-10-04T21:05:00Z",
    }


def starved_job(name, job_id):
    """The saturation shape: cancelled, no steps, no runner, no start time."""
    return job(name, conclusion="cancelled", steps=0, runner=None,
               started=None, job_id=job_id)


def needs(**results):
    """The `toJSON(needs)` payload, one entry per lane."""
    return json.dumps({k: {"result": v, "outputs": {}}
                       for k, v in results.items()})


def run_aggregate(needs_json, jobs=None, api_fails=False):
    """Execute the extracted aggregate program; return (rc, stdout, stderr).

    A real child interpreter with a real (fake) `gh` first on PATH, so the
    program's own code path -- including the exception handler around the API
    call -- runs unmodified rather than being simulated here.
    """
    tmp = tempfile.mkdtemp(prefix="merge-gate-aggregate-")
    try:
        gh = Path(tmp) / "gh"
        gh.write_text(FAKE_GH, encoding="utf-8")
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = {
            **os.environ,
            "PATH": f"{tmp}{os.pathsep}{os.environ.get('PATH', '')}",
            "MERGE_GATE_NEEDS": needs_json,
            "GITHUB_REPOSITORY": "maxi-tools/maxi-config",
            "RUN_ID": "3001",
            "RUN_ATTEMPT": "1",
            "FAKE_GH_JOBS": json.dumps(jobs or []),
        }
        if api_fails:
            env["FAKE_GH_FAIL"] = "1"
        else:
            env.pop("FAKE_GH_FAIL", None)
        proc = subprocess.run([sys.executable, "-c", aggregate_source()],
                              capture_output=True, text=True, env=env,
                              check=False)
        return proc.returncode, proc.stdout, proc.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class ExtractionTest(unittest.TestCase):
    """If the program cannot be lifted out, nothing below is evidence."""

    def test_the_heredoc_is_found_and_dedented(self):
        src = aggregate_source()
        self.assertIn("def starved(job):", src)
        self.assertIn("PASSING", src)
        # A YAML block scalar indents its content by the `run: |` key's column;
        # an undedented copy would fail to parse and every test would error.
        compile(src, "<aggregate>", "exec")

    def test_the_aggregate_still_fails_on_an_unrecognised_result(self):
        """The allowlist is the pre-existing safety property; keep it.

        A result GitHub adds later must mean stop, not proceed -- and the
        starvation classification must not become a hole in that.
        """
        rc, out, _ = run_aggregate(
            needs(plan="success", check="some_new_thing"),
            [job("check", conclusion="some_new_thing")])
        self.assertEqual(rc, 1, out)
        self.assertIn("::error::", out)


class StarvationClassificationTest(unittest.TestCase):
    """The fleet-vs-the-code distinction, on real payload shapes."""

    def test_a_saturated_pool_lane_is_labelled_capacity_not_failure(self):
        """The card's acceptance case, reproduced synthetically.

        `lane-plan` never got a runner, so the aggregate must say so as a
        warning -- never as `::error::`, which is the annotation a human and a
        reviewer both read as "your code is broken".
        """
        rc, out, _ = run_aggregate(
            needs(plan="cancelled", check="skipped", test="skipped",
                  package="skipped", **{"sign-publish": "skipped",
                                        "release-verify": "skipped"}),
            [starved_job("plan / lane-plan", 4242)])
        self.assertEqual(rc, 1, out)
        self.assertIn("::warning::", out)
        self.assertNotIn("::error::", out)
        self.assertIn("inconclusive-by-capacity: plan (job 4242)", out)
        self.assertIn("never scheduled", out)

    def test_a_concurrency_cancellation_stays_an_error(self):
        """The masking case: cancelled, but a runner WAS assigned.

        `runner-health.yml` sets `cancel-in-progress: true`, so superseded
        attempts cancel by design with a runner and steps recorded. Relabelling
        those as capacity pressure is exactly the wrong answer.
        """
        rc, out, _ = run_aggregate(
            needs(plan="cancelled", check="skipped", test="skipped"),
            [job("plan / lane-plan", conclusion="cancelled", steps=4)])
        self.assertEqual(rc, 1, out)
        self.assertIn("::error::", out)
        self.assertNotIn("inconclusive-by-capacity", out)

    def test_a_genuine_failure_beside_a_starvation_reports_both(self):
        """One lane was starved, another genuinely failed: both must be named.

        Reporting only the starved lane would hide a real red behind a capacity
        excuse, which is the direction this change must never drift.
        """
        rc, out, _ = run_aggregate(
            needs(plan="cancelled", test="failure", check="skipped"),
            [starved_job("plan / lane-plan", 1),
             job("test / lane-test", conclusion="failure", steps=9)])
        self.assertEqual(rc, 1, out)
        self.assertIn("::warning::", out)
        self.assertIn("::error::", out)
        self.assertIn("inconclusive-by-capacity: plan", out)

    def test_a_failure_whose_payload_is_absent_stays_a_failure(self):
        """An unmatched lane means the API shape changed; guessing is forbidden."""
        rc, out, _ = run_aggregate(
            needs(plan="cancelled", check="skipped", test="skipped"),
            [starved_job("something-else-entirely", 7)])
        self.assertEqual(rc, 1, out)
        self.assertIn("::error::", out)
        self.assertNotIn("inconclusive-by-capacity", out)

    def test_an_unreadable_api_falls_back_to_failing(self):
        """A 403 on the jobs endpoint must not silently disable the gate."""
        rc, out, _ = run_aggregate(
            needs(plan="cancelled", check="skipped", test="skipped"),
            [], api_fails=True)
        self.assertEqual(rc, 1, out)
        self.assertIn("::warning::could not read job payloads", out)
        self.assertIn("::error::", out)


class PassingRollupTest(unittest.TestCase):
    """The unchanged paths must be unchanged."""

    def test_all_green_passes(self):
        rc, out, _ = run_aggregate(
            needs(plan="success", check="success", test="success"),
            [job("plan / lane-plan")])
        self.assertEqual(rc, 0, out)
        self.assertIn("All required lanes passed", out)

    def test_skipped_lanes_are_fine(self):
        """Fan-out and docs-only PRs skip lanes deliberately."""
        rc, out, _ = run_aggregate(
            needs(plan="success", check="skipped", test="skipped"),
            [job("plan / lane-plan")])
        self.assertEqual(rc, 0, out)
        self.assertIn("skipped=['check', 'test']", out)

    def test_a_green_run_makes_no_api_call(self):
        """No non-passing lane means nothing to classify, so nothing is fetched.

        A green rollup must not depend on the Actions API being reachable: the
        classification is only needed when a lane is already non-passing, and
        paying a network round-trip on every green PR to learn that would make
        the gate less available, not more. Proven by making `gh` fail -- the
        rollup still passes, so the call never happened.
        """
        rc, out, err = run_aggregate(needs(plan="success", check="success"),
                                     api_fails=True)
        self.assertEqual(rc, 0, f"{out}\n{err}")
        self.assertNotIn("could not read job payloads", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
