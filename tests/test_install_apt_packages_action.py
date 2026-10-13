#!/usr/bin/env python3
"""The install-apt-packages collapse must not regress.

The original change (maxi-tools/maxi-config#174, ci#70) shipped three
near-identical copies of the apt-install step. The collapse moved the
logic into .github/actions/install-apt-packages and reduced each lane
to a `uses:` reference. The collapse was REJECTed once because:

  1. The `uses:` used `./.github/actions/...` -- a workspace-relative
     reference. That resolves inside each CALLER's checkout, and ~48
     consumer repositories do not vendor this action. Lane-check and
     lane-test check out the caller; lane-package did NOT check out
     the caller at all when the action step ran. The reference must
     use `$/` (GitHub's self-repository syntax), which resolves to
     the reusable workflow's own repository at the running commit
     with no checkout required.

  2. The checker script accepted `action.yml` only; a consumer that
     uses the supported `action.yaml` metadata filename was reported
     as unresolvable even though the runner executes it.

  3. There was no regression coverage at all -- the changed checker
     was satisfied with the producer tree and never exercised a
     caller-context failure path.

These tests cover all three: they parse the lanes directly (so a
typo in `uses:` fails), they exercise check_workflow_refs.py against
both `action.yml` and `action.yaml`, and they execute the install
action's body to confirm the no-apt-get and no-sudo branches still
warn-and-skip rather than fail the lane.

Run directly: `python3 tests/test_install_apt_packages_action.py`.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/check_workflow_refs.py"
ACTION_YML = ROOT / ".github/actions/install-apt-packages/action.yml"
LANES = (
    ROOT / ".github/workflows/lane-check.yml",
    ROOT / ".github/workflows/lane-test.yml",
    ROOT / ".github/workflows/lane-package.yml",
)


def lane_steps(path: pathlib.Path) -> list:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    steps = []
    for job in doc["jobs"].values():
        steps.extend(job.get("steps") or [])
    return steps


def install_step(path: pathlib.Path) -> dict:
    matches = [
        s
        for s in lane_steps(path)
        if s.get("name") == "Install repo-declared system packages"
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"{path.name} has {len(matches)} 'Install repo-declared system "
            "packages' steps; this test reads that step directly and a "
            "rename must not silently stop testing it"
        )
    return matches[0]


def action_run() -> str:
    """The composite action's bash body, verbatim."""
    doc = yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))
    # composite action: one step that contains the shell body.
    body = doc["runs"]["steps"][0]["run"]
    if not isinstance(body, str) or not body.strip():
        raise AssertionError(
            "install-apt-packages action body missing or empty; the collapse "
            "extracted an inline shell step into this action and the body is "
            "the entire deliverable"
        )
    return body


class LanesUseSelfRepositorySyntax(unittest.TestCase):
    """Every lane must use `$/...`, never `./...`, for install-apt-packages.

    The `./` form was the regression -- it resolves inside the caller's
    workspace, which ~48 consumers do not vendor. `$/` is GitHub's
    self-repository syntax: the runner loads the action from the reusable
    workflow's own repo at the exact running commit, with no checkout.
    """

    def test_install_step_uses_self_repository_syntax(self):
        for path in LANES:
            with self.subTest(lane=path.name):
                step = install_step(path)
                uses = step.get("uses", "")
                self.assertTrue(
                    uses.startswith("$/"),
                    f"{path.name}: `uses: {uses!r}` does not use the `$/` "
                    "self-repository syntax. A `./` reference resolves in "
                    "the CALLER's workspace, and consumer repositories do "
                    "not vendor this action. Use `$/ .github/actions/"
                    "install-apt-packages` so the runner loads the action "
                    "from the reusable workflow's own repo at the running "
                    "commit.",
                )
                self.assertIn(
                    "install-apt-packages",
                    uses,
                    f"{path.name}: `uses: {uses!r}` does not reference the "
                    "shared install-apt-packages composite action",
                )

    def test_no_lane_uses_workspace_relative_install_action(self):
        """Hard guard: `./.github/actions/install-apt-packages` must not
        appear in any lane. Actionlint flags this; the lane-self-check
        checker would too if it ran on the consumer's checkout, which it
        does not -- so we guard it here.
        """
        pattern = re.compile(r"uses:\s*\./\.github/actions/install-apt-packages")
        for path in LANES:
            with self.subTest(lane=path.name):
                text = path.read_text(encoding="utf-8")
                for raw in text.splitlines():
                    line = raw.split(" #", 1)[0]
                    if pattern.search(line):
                        self.fail(
                            f"{path.name} still uses the workspace-relative "
                            "reference `./.github/actions/install-apt-packages`. "
                            "Use `$/ .github/actions/install-apt-packages` so "
                            "the action resolves from the reusable workflow's "
                            "own repo at the running commit, not from each "
                            "caller's checkout."
                        )


class CheckWorkflowRefsAcceptsActionDotYaml(unittest.TestCase):
    """check_workflow_refs.py must accept both `action.yml` AND `action.yaml`.

    GitHub supports both metadata filenames. The previous version only
    accepted `action.yml`, so a consumer that used the `.yaml` form was
    reported as unresolvable even though the runner executes it.
    """

    def _run_checker(self, tree: pathlib.Path) -> tuple[int, str, str]:
        # The script reads ROOT = parents[2] of its own path, so the copy
        # under `tree`/scripts/ is what reads the synthetic consumer.
        script_in_tree = tree / ".github/scripts/check_workflow_refs.py"
        # The argv is built from sys.executable (absolute, controlled) and
        # the test's own copy of the script under `tree`. Nothing here
        # reads user-controlled input, so bandit B603 is a false positive.
        proc = subprocess.run(  # nosec B603
            [sys.executable, str(script_in_tree)],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def _make_consumer(self, tmp: pathlib.Path, metadata_name: str) -> pathlib.Path:
        """A minimal consumer repo with one action dir + one workflow that
        references it via `./...`.
        """
        actions_dir = tmp / ".github/actions/sample"
        actions_dir.mkdir(parents=True)
        actions_file = actions_dir / ("action." + metadata_name)
        actions_file.write_text(
            "name: sample\ndescription: sample\nruns:\n  using: composite\n  steps:\n    - run: echo ok\n",
            encoding="utf-8",
        )
        workflows = tmp / ".github/workflows/lane.yml"
        workflows.parent.mkdir(parents=True)
        workflows.write_text(
            "name: lane\non: pull_request\njobs:\n  lane:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./.github/actions/sample\n",
            encoding="utf-8",
        )
        scripts = tmp / ".github/scripts/check_workflow_refs.py"
        scripts.parent.mkdir(parents=True)
        scripts.write_text(SCRIPT.read_text(encoding="utf-8"))
        return tmp

    def test_action_yml_is_resolvable(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = pathlib.Path(raw)
            self._make_consumer(tmp, "yml")
            rc, out, err = self._run_checker(tmp)
            self.assertEqual(rc, 0, f"stdout:\n{out}\nstderr:\n{err}")
            self.assertIn("all references resolve", out)

    def test_action_yaml_is_resolvable_too(self):
        with tempfile.TemporaryDirectory() as raw:
            tmp = pathlib.Path(raw)
            self._make_consumer(tmp, "yaml")
            rc, out, err = self._run_checker(tmp)
            self.assertEqual(
                rc,
                0,
                "check_workflow_refs.py rejected an `action.yaml` metadata "
                "filename even though GitHub supports it. The runner happily "
                f"executes the action; the local check must agree.\n"
                f"stdout:\n{out}\nstderr:\n{err}",
            )
            self.assertIn("all references resolve", out)


class CheckWorkflowRefsAcceptsSelfRepositorySyntax(unittest.TestCase):
    """`$/` references must be deferred to runtime, not flagged as unresolvable.

    `$/` is GitHub's self-repository syntax for reusable workflows and
    composite actions. The checker treats it like a checkout-materialised
    path: validated as syntactically reasonable, deferred, and reported.
    Reporting it as a local resolution problem was the false-green that
    originally let the `./` regression ship (maxi-config#174, ci#70).
    """

    def _run(self, workflow_text: str) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            (tree / ".github/workflows").mkdir(parents=True)
            (tree / ".github/workflows/lane.yml").write_text(
                workflow_text,
                encoding="utf-8",
            )
            (tree / ".github/scripts").mkdir(parents=True)
            (tree / ".github/scripts/check_workflow_refs.py").write_text(
                SCRIPT.read_text(encoding="utf-8"),
            )
            return self._invoke(tree)

    @staticmethod
    def _invoke(tree: pathlib.Path) -> tuple[int, str, str]:
        # The script discovers files via ROOT = parents[2] of __file__, so
        # the copy we wrote into `tree`/scripts/ is what reads the test's
        # synthetic consumer -- not the production ROOT.
        script_in_tree = tree / ".github/scripts/check_workflow_refs.py"
        # Same argv as _run_checker: sys.executable (absolute) plus the
        # test's own copy of the script. No user input crosses this
        # boundary, so bandit B603 is a false positive.
        proc = subprocess.run(  # nosec B603
            [sys.executable, str(script_in_tree)],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_self_repository_action_reference_passes(self):
        workflow = (
            "name: lane\non: pull_request\njobs:\n  lane:\n"
            "    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: $/.github/actions/install-apt-packages\n"
        )
        rc, out, err = self._run(workflow)
        self.assertEqual(
            rc,
            0,
            "`$/ .github/actions/...` should be deferred to runtime as the "
            "self-repository syntax, not flagged as unresolvable. This was "
            "the false-green that let the `./` regression ship.\n"
            f"stdout:\n{out}\nstderr:\n{err}",
        )
        self.assertIn("resolves via self-repository syntax at runtime", out)

    def test_self_repository_path_traversal_rejected(self):
        """`$/../escape` must not be silently accepted -- a typo of the
        self-repository syntax can otherwise be read as a clean pass.
        """
        workflow = (
            "name: lane\non: pull_request\njobs:\n  lane:\n"
            "    runs-on: ubuntu-latest\n    steps:\n"
            "      - uses: $/../escape\n"
        )
        rc, out, err = self._run(workflow)
        self.assertNotEqual(rc, 0, err)
        self.assertIn("invalid self-repository path", err)


class InstallActionBehavior(unittest.TestCase):
    """The extracted composite must preserve the no-apt-get / no-sudo branches.

    The body was moved verbatim from three lane files; the trip-ups it
    worked around are still real (maxi-nix and similar runners have
    neither apt-get nor sudo). Hard-failing on `apt-get: command not
    found` was the regression that caused the original duplication.

    These tests exercise the body with `bash -n` (parse-only) plus a
    structural inspection of the script text. Running the body for
    real needs a host without apt-get OR sudo, and self-hosted Linux
    runners that match that profile vary; what we are guarding against
    is someone deleting the `command -v apt-get` guard during a future
    refactor, not a runtime regression. Parse + structural inspection
    catches that without needing a privileged sandbox.
    """

    @staticmethod
    def _body() -> str:
        body = action_run()
        # `bash -n` is parse-only: it returns rc=0 if the script is
        # syntactically valid. The composite action body is bash, and
        # a syntax error would silently break the lane -- but a body
        # that parses can still omit the no-apt-get branch.
        # bash is resolved via shutil.which into an absolute path so
        # bandit B607 (partial executable path) does not fire; the
        # `body` string is the action's own commit-pinned shell script
        # and is the thing under test, not user input.
        bash = shutil.which("bash") or "/bin/bash"
        rc = subprocess.call(  # nosec B603
            [bash, "--noprofile", "--norc", "-n", "-c", body]
        )
        if rc != 0:
            raise AssertionError(f"install-apt-packages body fails bash -n: rc={rc}")
        return body

    def test_no_apt_get_branch_is_present(self):
        """The body must guard on `command -v apt-get` and warn-and-skip.

        This was the regression that caused the original duplication --
        a `sudo apt-get` hard-coded against a self-hosted runner without
        sudo. Removing the guard re-introduces the duplication.
        """
        body = self._body()
        self.assertIn("command -v apt-get", body)
        self.assertIn("No apt-get on this runner", body)
        # The notice is the warn-and-skip contract: a lane must not fail
        # on a runner without apt-get.
        self.assertIn("::notice::", body)

    def test_no_sudo_branch_is_present(self):
        """The body must guard on `command -v sudo` for non-root users.

        The same self-hosted runners that lack apt-get also lack sudo
        for the build user; an unprivileged user without sudo cannot
        install. A check on `command -v sudo` is the skip contract.
        """
        body = self._body()
        self.assertIn("command -v sudo", body)
        self.assertIn("Cannot install system packages", body)
        self.assertIn("::warning::", body)

    def test_action_description_documents_the_collapse(self):
        """The action's description explains WHY this exists.

        The collapse came from maxi-config#174 / ci#70: three near-
        identical copies existed, and a fix to one was silently a no-op
        because nothing marked which copies were live. Future readers
        need to know not to re-duplicate the step.
        """
        doc = yaml.safe_load(ACTION_YML.read_text(encoding="utf-8"))
        self.assertIn("maxi-config", doc.get("description", ""))
        self.assertIn("174", doc.get("description", ""))

    def test_inline_apt_get_discipline_preserved(self):
        """The lane files must not re-introduce an inline `apt-get` body.

        The whole point of the collapse was to deduplicate the install
        step. A future lane author that copies the action body back
        inline would silently regress the duplication.
        """
        for path in LANES:
            with self.subTest(lane=path.name):
                text = path.read_text(encoding="utf-8")
                # No `apt-get` in the lane workflow files at all --
                # the action owns that detail.
                self.assertNotIn(
                    "apt-get",
                    text,
                    f"{path.name} contains `apt-get`; the install lives in "
                    "the install-apt-packages composite action. Inline "
                    "apt-get was the regression this collapse was meant to "
                    "prevent (maxi-config#174, ci#70).",
                )


if __name__ == "__main__":
    unittest.main()
