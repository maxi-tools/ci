#!/usr/bin/env python3
"""The moments gallery stays inside GitHub's summary cap and reports Pages.

The verifier of ci#7 reproduced a 4,197,472-byte job summary from one
valid PNG, past the 1 MiB per-step cap, and found no `pages` input and no
lane or latest-deployment output. This runs the renderer's own self-test,
which covers both, so the lane fails if either regresses.

Run directly: `python3 tests/test_moments_gallery.py`.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
RENDERER = ROOT / ".github/actions/moments-gallery/moments_gallery.py"
ACTION = ROOT / ".github/actions/moments-gallery/action.yml"


class MomentsGalleryTest(unittest.TestCase):
    def test_self_test_passes(self) -> None:
        result = subprocess.run(
            [sys.executable, str(RENDERER), "--self-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"self-test failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn("SELF_TEST_OK", result.stdout)
        # The large-frame case must have actually run, not been skipped.
        self.assertIn("SELF_TEST large png:", result.stdout)

    def test_action_declares_the_pages_contract(self) -> None:
        text = ACTION.read_text(encoding="utf-8")
        for needle in (
            "\n  pages:\n",
            "pages_enabled",
            "\n  lane:\n",
            "\n  latest_deployment:\n",
            "gallery/${LANE}/latest/",
            "actions/upload-pages-artifact@",
            "actions/deploy-pages@",
        ):
            self.assertIn(needle, text, f"action.yml missing {needle!r}")


if __name__ == "__main__":
    unittest.main()
