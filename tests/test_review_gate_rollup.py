"""Regression: an early unreviewed run must not poison a later reviewed SHA."""
from pathlib import Path
import unittest

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/review-gate-reusable.yml"


class ReviewGateRollup(unittest.TestCase):
    def test_failed_then_reviewed_has_green_rollup(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        gate = workflow["jobs"]["gate"]
        steps = gate["steps"]
        action = next(s for s in steps if s.get("id") == "gate")
        publish = next(s for s in steps if s.get("name") == "Publish the verdict as a commit status")
        contexts = {leg["status_context"] for leg in gate["strategy"]["matrix"]["include"]}
        self.assertEqual(contexts, {"review-gate/threads", "review-gate/non-author-review"})
        self.assertTrue(action["continue-on-error"])
        # conclusion is success after continue-on-error; outcome remains failure.
        self.assertEqual(publish["env"]["GATE_OUTCOME"], "${{ steps.gate.outcome }}")
        self.assertRegex(publish["run"], r"failure\)\s+state=failure")
        self.assertRegex(publish["run"], r"success\)\s+state=success")
        self.assertTrue(publish["continue-on-error"])
        self.assertEqual(publish["if"], "${{ !cancelled() }}")

        # GitHub keeps *all* check suites for a SHA, but only the newest
        # commit status per context. First run has no review; second has one.
        check_rows = []
        statuses = {}
        for reviewed in (False, True):
            outcome = "success" if reviewed else "failure"
            check_rows.append("success" if action["continue-on-error"] else outcome)
            statuses["review-gate/non-author-review"] = outcome
            statuses["review-gate/threads"] = "success"
        self.assertEqual(check_rows, ["success", "success"])
        self.assertEqual(set(statuses.values()), {"success"})
        self.assertTrue(all(row == "success" for row in [*check_rows, *statuses.values()]))


if __name__ == "__main__":
    unittest.main()
