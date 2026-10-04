#!/usr/bin/env python3
"""The compiled-inputs gate this repository publishes, RUN not read.

maxi-core's ci_rust_touched.py answers "can this diff reach rustc" for
one repository. This action publishes the same answer for every
consumer, which is what makes the generalization risky in the two
places these tests pin:

  1. The direction. `False` means a compile lane does not run, so any
     uncertainty must resolve to `True`. An unreadable workflow
     directory, an empty diff, an unparseable include -- each has a test
     asserting it widens rather than skips.

  2. The build-defining-workflow DETECTION, which replaced maxi-core's
     hardcoded `.github/workflows/ci.yml`. A consumer's heavy lanes can
     live in rust_test.yml and rust_build.yml (freya's do), and the
     classifier must find them by content, not by name -- while still
     classifying the fan-out's review-gate.yml pin bump, the measured
     277-Linux/22-macOS/3-Windows-job waste this action exists to
     reclaim, as orchestration that cannot reach rustc.

The tests build small fake repositories on disk because the classifier
reads the tree (workflows, .gitmodules, include sites); a pure-function
test would not exercise the half that can silently fail.

Run directly: `python3 tests/test_rust_touched_gate.py`.
"""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
ACTION = HERE.parent / ".github/actions/rust-touched"


def load():
    spec = importlib.util.spec_from_file_location(
        "rust_touched", ACTION / "rust_touched.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - importlib contract
        raise RuntimeError("importlib could not load the classifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load()


def consumer_repo(root: pathlib.Path) -> pathlib.Path:
    """A minimal freya-shaped consumer: workflows, submodule, justfile."""
    wf = root / ".github/workflows"
    wf.mkdir(parents=True)
    # The fan-out target: a pin bump in a workflow with no Rust content.
    (wf / "review-gate.yml").write_text(
        "jobs:\n  review-gate:\n    uses: maxi-tools/ci/.github/workflows/"
        "review-gate-reusable.yml@2d93"
        "da95eb16\n",
        encoding="utf-8",
    )
    # Heavy lanes, freya's names.
    (wf / "rust_test.yml").write_text(
        "jobs:\n  build:\n    steps:\n"
        "    - uses: dtolnay/rust-toolchain@"
        "1.94\n"
        "    - run: just t\n",
        encoding="utf-8",
    )
    (wf / "rust_build.yml").write_text(
        "jobs:\n  build:\n    steps:\n"
        "    - uses: actions/checkout@v6\n"
        "    - run: cargo build --release\n",
        encoding="utf-8",
    )
    (root / ".gitmodules").write_text(
        '[submodule "lucide"]\n'
        "\tpath = crates/freya-icons/external/lucide\n"
        "\turl = https://github.com/lucide-icons/lucide\n",
        encoding="utf-8",
    )
    (root / "justfile").write_text("t:\n    cargo nextest run\n", encoding="utf-8")
    (root / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    return root


class WidensOnUncertainty(unittest.TestCase):
    """Every judgement resolves towards running the lanes."""

    def decide(self, changed, root):
        return rt.decide(rt.changed_paths(iter([l + "\n" for l in changed])), root)

    def test_empty_diff_widens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = consumer_repo(pathlib.Path(tmp))
            needed, why = self.decide([], root)
            self.assertTrue(needed)
            self.assertIn("no changed files", why)

    def test_unreadable_workflows_dir_widens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = consumer_repo(pathlib.Path(tmp))
            (root / ".github/workflows").chmod(0o000)
            try:
                needed, why = self.decide(["docs/x.md"], root)
                self.assertTrue(needed)
                self.assertIn("could not read", why)
            finally:
                (root / ".github/workflows").chmod(0o755)

    def test_none_of_the_workflows_dir_widens_for_source(self):
        """No workflows directory at all: source still widens by name."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "src").mkdir()
            (root / "src/lib.rs").write_text("fn main() {}\n", encoding="utf-8")
            needed, why = self.decide(["src/lib.rs"], root)
            self.assertTrue(needed)
            self.assertIn("Rust source", why)


class PinOnlyDiffsSkip(unittest.TestCase):
    """THE saving: CI orchestration that cannot reach rustc."""

    def decide(self, changed, root):
        return rt.decide(rt.changed_paths(iter([l + "\n" for l in changed])), root)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = consumer_repo(pathlib.Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_review_gate_pin_bump_skips(self):
        """The measured case: a one-line pin bump in review-gate.yml."""
        needed, why = self.decide([".github/workflows/review-gate.yml"], self.root)
        self.assertFalse(needed, why)
        self.assertIn("can reach rustc", why)

    def test_docs_only_skips(self):
        needed, why = self.decide(["README.md", "docs/design.md"], self.root)
        self.assertFalse(needed, why)

    def test_release_drafter_and_spelling_skip(self):
        for name in ("release-drafter.yml", "spelling.yml", "typing-errors.yml"):
            (self.root / ".github/workflows" / name).write_text(
                "jobs: {}\n", encoding="utf-8"
            )
            needed, _ = self.decide([f".github/workflows/{name}"], self.root)
            self.assertFalse(needed, name)


class BuildDefiningWidens(unittest.TestCase):
    """A diff that touches the build, in any consumer's spelling of it."""

    def decide(self, changed, root):
        return rt.decide(rt.changed_paths(iter([l + "\n" for l in changed])), root)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = consumer_repo(pathlib.Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def test_rust_source_widens(self):
        (self.root / "crates").mkdir()
        (self.root / "crates/lib.rs").write_text("fn f() {}\n", encoding="utf-8")
        needed, why = self.decide(["crates/lib.rs"], self.root)
        self.assertTrue(needed)
        self.assertIn("Rust source", why)

    def test_the_repos_own_heavy_workflows_widen(self):
        for name in ("rust_test.yml", "rust_build.yml"):
            needed, why = self.decide([f".github/workflows/{name}"], self.root)
            self.assertTrue(needed, name)
            self.assertIn("defines the build", why)

    def test_the_ci_wrapper_widens(self):
        """The thin `uses: .../rust-ci.yml@sha` wrapper is the lanes."""
        (self.root / ".github/workflows/ci.yml").write_text(
            "jobs:\n  merge-gate:\n    uses: maxi-tools/ci/.github/workflows/"
            "rust-ci.yml@e8a4"
            "eec2da9c\n",
            encoding="utf-8",
        )
        needed, why = self.decide([".github/workflows/ci.yml"], self.root)
        self.assertTrue(needed)
        self.assertIn("defines the build", why)

    def test_manifests_lockfiles_and_justfile_widen(self):
        for path in (
            "Cargo.lock",
            "crates/x/Cargo.toml",
            "justfile",
            "rust-toolchain.toml",
            "clippy.toml",
            "rustfmt.toml",
        ):
            with self.subTest(path=path):
                needed, reason = self.decide([path], self.root)
                self.assertTrue(needed, reason)

    def test_submodule_pointer_widens(self):
        needed, why = self.decide(["crates/freya-icons/external/lucide"], self.root)
        self.assertTrue(needed)
        self.assertIn("submodule", why)

    def test_editing_the_submodule_map_widens(self):
        needed, why = self.decide([".gitmodules"], self.root)
        self.assertTrue(needed)

    def test_github_actions_dir_widens(self):
        needed, why = self.decide(
            [".github/actions/linux-system-deps/action.yml"], self.root
        )
        self.assertTrue(needed)
        self.assertIn("defines the build", why)

    def test_script_a_heavy_workflow_runs_widens(self):
        (self.root / ".github/workflows/rust_test.yml").write_text(
            "jobs:\n  build:\n    steps:\n"
            "    - uses: dtolnay/rust-toolchain@"
            "1.94\n"
            "    - run: python3 .github/scripts/check_something.py\n",
            encoding="utf-8",
        )
        (self.root / ".github/scripts").mkdir(parents=True)
        (self.root / ".github/scripts/check_something.py").write_text(
            "", encoding="utf-8"
        )
        needed, why = self.decide([".github/scripts/check_something.py"], self.root)
        self.assertTrue(needed)
        self.assertIn("run by a lane", why)


class CompiledInputsStillGuard(unittest.TestCase):
    """The include index travels with the gate; it must still bite."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = consumer_repo(pathlib.Path(self._tmp.name))
        self.addCleanup(self._tmp.cleanup)

    def decide(self, changed):
        return rt.decide(rt.changed_paths(iter([l + "\n" for l in changed])), self.root)

    def test_an_included_asset_widens(self):
        (self.root / "src").mkdir()
        (self.root / "src/main.rs").write_text(
            'const G: &str = include_str!("../docs/EMOJI_GUIDE.md");\n',
            encoding="utf-8",
        )
        needed, why = self.decide(["docs/EMOJI_GUIDE.md"])
        self.assertTrue(needed)
        self.assertIn("compiled in", why)

    def test_an_unresolvable_include_widens(self):
        (self.root / "src").mkdir()
        (self.root / "src/main.rs").write_text(
            'const G: &str = include_str!(concat!(env!("OUT_DIR"), "/x"));\n',
            encoding="utf-8",
        )
        # OUT_DIR sites are resolvable-to-nothing, not unresolved; the
        # source itself is what widens here.
        needed, _ = self.decide(["src/main.rs"])
        self.assertTrue(needed)


class RealRepositorySmoke(unittest.TestCase):
    """Run the classifier against THIS repository's own tree.

    This repo has no Cargo workspace (rust-ci.yml's own header says so),
    so a self-diff of docs and CI files must skip, and the lane files
    that name cargo must widen. A cheap guard that the detection rules
    hold on a real tree, not only the fixtures above.
    """

    ROOT = HERE.parent

    def test_a_workflow_only_diff_of_this_repo_skips(self):
        needed, why = rt.decide(["docs/ci-design.md"], self.ROOT.resolve())
        self.assertFalse(needed, why)

    def test_a_lane_file_of_this_repo_widens(self):
        needed, why = rt.decide(
            [".github/workflows/lane-check.yml"], self.ROOT.resolve()
        )
        self.assertTrue(needed)
        self.assertIn("defines the build", why)


if __name__ == "__main__":
    unittest.main()
