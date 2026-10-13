#!/usr/bin/env python3
"""Can this changeset affect the Rust build?

Answers one question, for any consumer repository: may a lane that compiles
Rust skip itself for this diff? Ported from maxi-core's
`.github/scripts/ci_rust_touched.py` (measured there on the last 25 merged
PRs: 12 touched no Rust and still spent 352 runner-minutes in those lanes,
29 minutes each), generalized in exactly two ways and unchanged everywhere
else.

WHY THIS IS NOT `paths-ignore`

A workflow-level `paths-ignore` skips the whole workflow, so a required
context never reports and the pull request blocks forever. A job-level
`if:` runs the workflow and reports the job as `skipped`, which satisfies
a required check, so the same saving needs no stub and mixed diffs need
no ordering rule.

And a path filter written from the directory layout cannot see a
dependency made by a macro. `include_str!` makes a data file part of the
build as surely as a `.rs` file is, and nothing in the layout says so.
See `ci_compiled_inputs.py` (vendored beside this file, byte-for-byte
from maxi-core) for the index that closes that hole.

WHAT "BUILD-DEFINING WORKFLOW" MEANS HERE, AND WHY IT IS DETECTED RATHER
THAN LISTED

maxi-core's copy hardcodes `.github/workflows/ci.yml`: the one workflow
that chooses the toolchain, the flags and the runner for its lanes. A
shared action cannot hardcode one name for every consumer -- freya's
heavy lanes live in `rust_test.yml`, `rust_build.yml`, `rust_lint.yml`
and `rust_android.yml`, and the next consumer differs again.

So the rule is derived at run time: a workflow is build-defining when its
text names the Rust toolchain (`cargo`, `rustc`, `rust-toolchain`,
`nextest`, `clippy`, `rustfmt`, `rust-cache`, ...). The test is over the
WHOLE file, comments included, deliberately: a commented-out cargo step
is one deleted comment away from a live one, and classifying a workflow
as build-defining when it is not costs a CI run, while the reverse costs
the premise. Editing a build-defining workflow runs every lane; editing
any other workflow (CI orchestration that cannot reach rustc -- the
review-gate pin a fan-out advances is the measured example: 48 consumer
PRs, 277 Linux + 22 macOS + 3 Windows jobs, for a one-line pin bump)
skips them.

THE DIRECTION ERRORS MUST FALL

`False` here means a compile lane does not run. A wrong `False` is a
green check on an untested build, so every judgement is resolved
towards `True`: an unreadable diff, an unreadable index, a
`include_str!` whose argument is not a literal, a path that cannot be
classified -- each returns `True` with a reason. Being wrong towards
`True` costs runner minutes. Being wrong towards `False` costs the
premise.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from ci_compiled_inputs import compiled_inputs  # noqa: E402

# Changes that are the build, or decide how it runs.
RUST_SUFFIX = (".rs",)
RUST_EXACT = {
    "Cargo.lock",
    "rust-toolchain",
    "rust-toolchain.toml",
    # A submodule pointer is compiled code: the PR that moves
    # crates/freya-icons/external/lucide changes what every build
    # produces, and no `.rs` rule or include index would name it.
    ".gitmodules",
    # `run: just t` hands the whole build to the justfile; an edit to
    # it changes what every lane executes without touching a `.rs`.
    "justfile",
    "Justfile",
}
RUST_PREFIX = (".cargo/", ".github/actions/")
# Subtrees whose contents are read at BUILD or TEST time by conventions
# this action cannot see statically. A build script consuming
# proto/api.proto (tonic_build) changes generated Rust when the proto
# changes; a test reading tests/fixtures/case.json changes its verdict
# when the fixture changes. Neither matches a name rule or an
# include_* site, so the SUBTREE widens -- over-inclusive in the
# direction that costs minutes, not premise. `ci/` because the shared
# lanes read consumer-side files there (apt-packages.txt et al).
RUST_SUBTREE_WIDEN = (
    "proto/",
    "protos/",
    "tests/fixtures/",
    "test_fixtures/",
    "ci/",
)
# Basename tokens marking a workflow as a build or test lane even when
# the post-change tree no longer classifies it as build-defining (the
# rust usage was just removed from it, or it was deleted). Paired with
# the .github/workflows/ rule in build_defining so defanging or
# deleting a heavy workflow widens instead of silently narrowing.
HEAVY_WORKFLOW_TOKENS = ("build", "test", "lint", "android")
# A manifest or build script under any member, not just the root.
# `clippy.toml` changes the `cargo clippy` contract wherever in the tree
# it sits, so it is matched by basename like a manifest.
# `Cargo.toml` is basename rather than exact: a workspace member under
# crates/foo with its own manifest is as build-defining as the root one.
# The rustfmt variants reach only the formatter, but the formatter runs
# inside a lane this gate can skip, which is the same false green.
RUST_BASENAME = {
    "Cargo.toml",
    "build.rs",
    "clippy.toml",
    "rustfmt.toml",
    ".rustfmt.toml",
}

# A workflow whose ACTIVE lines name the Rust toolchain is the build.
# Active lines only, because whole-file matching lost to real prose on
# the first run against freya: review-gate.yml says "spends, just what
# the gate job needs" in a comment and maxi-review.yml mentions
# Cargo.toml in a shell case arm, and both were classified
# build-defining -- which would have made the fan-out's pin bump run
# every lane anyway. Comment-only lines are dropped; TRAILING comments
# are kept, because stripping `echo x # cargo` would under-mark in the
# unsafe direction.
# Lowercase `\bcargo\b` (not `Cargo.toml`): a case arm like
# `case ... in Cargo.toml|Cargo.lock)` is classification logic, not a
# build step.
# `rust-ci\.yml` catches the thin wrapper that only says
# `uses: maxi-tools/ci/.github/workflows/rust-ci.yml@<sha>` and nothing
# else: the wrapper IS the lane composition for that repository.
BUILD_MARKER = re.compile(
    r"\bcargo\b|\brustc\b|\brustup\b|rust-toolchain|\bnextest\b"
    r"|\bclippy\b|\brustfmt\b|rust-cache|sccache|muslrust"
    r"|dtolnay/rust-toolchain|taiki-e/install-action|rust-ci\.yml"
)
# `just` in command position only: line start, or after the shell
# separators/YAML colon that precede a command. `it just asked` in
# prose has a word character before the space, which this rejects.
# Defined ONCE, beside BUILD_MARKER: an earlier edit left a second,
# looser definition later in the file that silently overrode this
# one -- the exact silent no-op this repo's tests exist to catch.
JUST_REF = re.compile(r"(?:^|[:;&|`(]\s*)just\s+\w")
WORKFLOW_GLOB = ".github/workflows"


def _active_text(text: str) -> str:
    """`text` with whole-line comments blanked, offsets preserved."""
    out = []
    for line in text.splitlines():
        out.append("" if line.lstrip().startswith("#") else line)
    return "\n".join(out)


# Scripts the build-defining workflows run, read out of those files
# rather than listed here. A script that only a gated lane executes is
# never exercised by any other lane, so editing it alone would skip the
# one job that runs it. Derived because a hand-list is wrong the moment
# a step is added (maxi-core's review found two such scripts missing
# from an earlier hand-list, which is the argument for not keeping one).
SCRIPT_REF = re.compile(
    r"((?:\.github/(?:scripts|actions)|scripts)/[\w./-]+\.(?:sh|py))"
)
ACTION_REF = re.compile(r"uses:\s*\.??/?\.github/actions/([\w./-]+)")


def build_defining_workflows(root: pathlib.Path) -> set[str]:
    """Workflow paths whose text names the Rust toolchain.

    Fails toward widening on unreadable files by letting the OSError
    propagate to the caller's widen-all handler: a workflow this cannot
    read is a workflow it must treat as build-defining.
    """
    found: set[str] = set()
    workflows = root / WORKFLOW_GLOB
    if not workflows.is_dir():
        # No workflows directory means no workflow can have been edited;
        # the empty set is the honest answer, not a failed lookup.
        return found
    for path in workflows.iterdir():
        if not path.is_file() or path.suffix not in (".yml", ".yaml"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        active = _active_text(text)
        if BUILD_MARKER.search(active) or JUST_REF.search(active):
            found.add(str(path.relative_to(root)))
    return found


def gated_lane_inputs(root: pathlib.Path) -> set[str]:
    """Scripts and actions the build-defining workflows run.

    Every path here is one an edit to which must re-run the lanes, on
    the same grounds as the workflow file itself: the lane executes it.
    Broad on purpose -- a mention in a comment counts, because a
    commented step is one deleted comment away from a live one and the
    failure direction of this whole module is "run the lane".
    """
    found: set[str] = set()
    for workflow in build_defining_workflows(root):
        text = (root / workflow).read_text(encoding="utf-8", errors="replace")
        found.update(m.rstrip("/") + "/" for m in ACTION_REF.findall(text))
        for ref in SCRIPT_REF.findall(text):
            # removeprefix, NOT lstrip: lstrip("./") eats the leading
            # `.` of `.github` itself, producing `github/scripts/...`,
            # a path no changed-file list ever contains -- which turned
            # this whole rule into a silent no-op the first time it ran.
            ref = ref.removeprefix("./")
            found.add(ref)
    return found


def submodule_paths(root: pathlib.Path) -> set[str]:
    """Paths `.gitmodules` declares, parsed rather than guessed.

    A submodule pointer IS compiled code: the PR that moves
    crates/freya-icons/external/lucide changes what every build
    produces, and no `.rs` rule or include index would name it. The
    checkout may not have materialised the directory (submodules:
    false), so presence on disk cannot be the test -- the map is the
    test. Unparseable .gitmodules returns an empty set: the exact-match
    rule below still lists `.gitmodules` itself, so editing the map
    widens even when reading it failed.
    """
    map_file = root / ".gitmodules"
    found: set[str] = set()
    if not map_file.is_file():
        return found
    for line in map_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("path"):
            _, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if value:
                found.add(value)
    return found


def build_defining(
    path: str, root: pathlib.Path, workflows: set[str], submodules: set[str]
) -> str | None:
    """Why this path is the build, or None if not decided by name alone.

    Split out of `decide` so each answer keeps its own early return and
    its own sentence. Every `True` here says WHICH rule fired, and that
    reason is what reaches the check summary when a lane runs.
    """
    if path.endswith(RUST_SUFFIX):
        return f"{path} is Rust source"
    if path in RUST_EXACT or path in workflows:
        why = (
            "defines the build"
            if path in workflows
            else "is a manifest, lockfile or submodule map"
        )
        return f"{path} {why}"
    if path in submodules:
        return f"{path} is a submodule of this repository"
    if path.startswith(RUST_PREFIX):
        return f"{path} defines the build"
    if path.startswith(RUST_SUBTREE_WIDEN):
        return (
            f"{path} is build or test input read by convention "
            "(proto source, fixture, or consumer-side CI file)"
        )
    if path.startswith(".github/workflows/"):
        # A heavy workflow EDITED OFF the build-defining set (its rust
        # usage removed, or the file deleted outright) is not in
        # `workflows` -- computed from the post-change tree -- so the
        # set-based rules above stay silent exactly when the workflow
        # that runs the lanes is the thing being weakened. Match the
        # basename: orchestration files (review-gate, spelling,
        # release-drafter) stay skippable, anything named like a build
        # or test lane widens.
        base = pathlib.PurePosixPath(path).name
        if any(token in base.lower() for token in HEAVY_WORKFLOW_TOKENS):
            return f"{path} is a build or test lane workflow"
    if pathlib.PurePosixPath(path).name in RUST_BASENAME:
        return f"{path} is a manifest or build script"
    target = root / path
    if path and not pathlib.PurePosixPath(path).suffix and target.is_dir():
        return f"{path} is a directory-level change (submodule pointer or tree)"
    return None


def decide(changed: list[str], root: pathlib.Path) -> tuple[bool, str]:
    """(needs the Rust lanes, why). Never returns False on uncertainty."""
    if not changed:
        # An empty diff is not proof of a harmless one -- it is far more
        # likely that computing the diff failed.
        return True, "no changed files reported; refusing to infer a safe diff"

    try:
        workflows = build_defining_workflows(root)
    # Widening on ANY failure is the point; narrowing this to the
    # exceptions we thought of is what would let an unanticipated one
    # skip a lane.
    except Exception as err:  # noqa: BLE001 -- any failure must widen
        return True, f"could not read which workflows define the build ({err})"

    submodules = submodule_paths(root)

    for path in changed:
        reason = build_defining(path, root, workflows, submodules)
        if reason:
            return True, reason

    return _decide_by_index(changed, root, workflows)


# A rule returns the verdict it reached, or None to mean "this rule did
# not fire, ask the next one". Never `(False, ...)` for that: a rule
# saying "not my business" and the gate saying "nothing here can reach
# rustc" are different claims, and collapsing them would let a rule that
# merely fell through skip a lane.
def _lane_script_rule(
    changed: list[str], root: pathlib.Path, workflows: set[str]
) -> tuple[bool, str] | None:
    """Widen if the diff edits a script a gated workflow actually runs."""
    try:
        lane_inputs = gated_lane_inputs(root)
    except Exception as err:  # noqa: BLE001 -- any failure must widen
        return True, f"could not read which scripts the gated lanes run ({err})"
    for path in changed:
        if path in lane_inputs or any(
            path.startswith(prefix) for prefix in lane_inputs
        ):
            return True, f"{path} is run by a lane this gate can skip"
    return None


def _crate_dir(current: pathlib.Path, root: pathlib.Path) -> str:
    """The crate subtree `current` sits in, "" when it is the root crate.

    Shared by the unresolved-site walk and the invoker scan. The
    repository root counts as a crate boundary when a root Cargo.toml
    exists (single-crate layout): `relative_to(root)` yields "." there,
    which no changed path ever equals, so recording it verbatim would
    make the widening rules never fire for root crates -- exactly the
    false skip they exist to prevent.
    """
    rel = current.relative_to(root).as_posix()
    # "." (the root crate) prefixes nothing; "" prefixes everything,
    # which is the honest meaning for a crate whose manifest sits at
    # the repository root.
    return "" if rel == "." else rel


def _unresolved_crates(unresolved: list[str], root: pathlib.Path) -> list[str]:
    """Crate directories containing the unresolvable include sites.

    Each entry of `unresolved` is `path:line: note` or `path: note`,
    repo-relative. The crate is found by walking up from the site to
    the nearest ancestor holding a Cargo.toml -- the same boundary
    cargo uses. The REPOSITORY ROOT counts as a crate boundary when a
    root Cargo.toml exists (single-crate layout): `relative_to(root)`
    yields "." there, which no changed path ever equals, so recording
    it verbatim would make this rule never fire for root crates --
    exactly the false skip it exists to prevent. Sites with no crate
    ancestor at all widen the whole tree: their asset directory could
    be anywhere, and the failure direction of this module is
    run-the-lane.
    """
    crates: set[str] = set()
    whole_tree = False
    for note in unresolved:
        site = note.split(":", 1)[0]
        probe = root / site
        if not probe.is_file():
            whole_tree = True
            continue
        current = probe.parent
        while True:
            if (current / "Cargo.toml").is_file():
                crates.add(_crate_dir(current, root))
                break
            if current == root or current.parent == current:
                whole_tree = True
                break
            current = current.parent
    if whole_tree:
        # "" prefixes everything, so _in_crates matches every path.
        return [""]
    return sorted(crates)


def _in_crates(path: str, crates: list[str]) -> str | None:
    """The crate whose subtree contains `path`, or None."""
    for crate in crates:
        if not crate or path == crate or path.startswith(crate + "/"):
            return crate or "(the whole tree)"
    return None


def _macro_name_at(site: pathlib.Path, line_no: int) -> str | None:
    """The macro_rules! a site sits inside, or None.

    `line_no` is 1-based, matching the notes' format. The innermost
    definition BEFORE the site on the same file wins; a site outside
    any macro returns None. Comment-only matches are harmless here:
    a phantom name just widens whatever crates invoke a macro of that
    name, and widening is the safe direction.
    """
    try:
        text = site.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prefix = "\n".join(text.split("\n")[: max(line_no, 1)])
    found = None
    for match in re.finditer(r"macro_rules!\s*(\w+)", prefix):
        found = match.group(1)
    return found


def _invoker_crates(root: pathlib.Path, macro: str) -> set[str]:
    """Crates whose Rust invokes `macro` -- asset paths may live there.

    A `macro_rules!` body with `include_bytes!($path)` resolves its
    argument at each INVOCATION, so the asset can sit beside any
    caller, not inside the defining crate. The invocation sites name
    their crates; those subtrees widen too.
    """
    pattern = re.compile(rf"\b{re.escape(macro)}\s*!")
    crates: set[str] = set()
    for path in root.rglob("*.rs"):
        # Vendored/external trees are not part of the build's source.
        parts = path.relative_to(root).parts
        if any(p in ("target", "external") for p in parts[:-1]):
            continue
        try:
            if pattern.search(path.read_text(encoding="utf-8", errors="replace")):
                current = path.parent
                while True:
                    if (current / "Cargo.toml").is_file():
                        crates.add(_crate_dir(current, root))
                        break
                    if current == root or current.parent == current:
                        crates.add("")
                        break
                    current = current.parent
        except OSError:
            crates.add("")
    return crates


def _compiled_input_rule(
    changed: list[str], root: pathlib.Path, workflows: set[str]
) -> tuple[bool, str] | None:
    """Widen if the diff edits a file some crate compiles in, or if we cannot tell.

    `workflows` is unused: the rules in INDEX_RULES share one signature so
    the loop can call any of them, and splitting the index half from the
    name-only half (which does need it) is what keeps each sentence
    attributable to the rule that fired.
    """
    try:
        compiled, unresolved = compiled_inputs(root)
    except Exception as err:  # noqa: BLE001 -- any failure must widen
        traceback.print_exc()
        return True, f"could not index compiled-in files ({err})"

    if unresolved:
        # A site whose argument cannot be read statically (the measured
        # example: freya-icons' `include_bytes!($path)` inside a
        # macro_rules! whose invocations its build script generates)
        # names a file this cannot compute. Treating that as "no file"
        # is the omission that produces a false green. Treating it as
        # "every file" is the opposite failure, measured on freya: the
        # gate goes permanently inert and the skip this action exists
        # to deliver never fires. The middle that stays sound in both
        # directions: the site lives in a crate whose subtree contains
        # the assets it reads (build scripts read beside themselves),
        # so the subtree is what widens. A diff OUTSIDE every such
        # crate still skips; a diff inside one runs every lane.
        crates = _unresolved_crates(unresolved, root)
        # Invokers of the macros those sites sit in widen too: a
        # macro's include resolves at each call site, so the asset can
        # live beside a caller outside the defining crate.
        for note in unresolved:
            parts = note.split(":", 2)
            if len(parts) >= 2 and parts[1].isdigit():
                macro = _macro_name_at(root / parts[0], int(parts[1]))
                if macro:
                    crates = sorted(set(crates) | _invoker_crates(root, macro))
        for path in changed:
            crate = _in_crates(path, crates)
            if crate:
                return True, (
                    f"{path} is inside {crate}, whose include sites "
                    f"are not statically resolvable: {unresolved[0]}"
                )
        # Outside them, the readable index still governs.
        for path in changed:
            if path in compiled:
                return (
                    True,
                    f"{path} is compiled in via include_str!/include_bytes!/include!",
                )
        return None

    for path in changed:
        if path in compiled:
            return (
                True,
                f"{path} is compiled in via include_str!/include_bytes!/include!",
            )
    return None


# In order, and the order is load-bearing only in that the first rule to
# fire supplies the reason. Both widen, so a diff that trips both is
# widened either way; the lane-script rule goes first because it is the
# cheaper read.
INDEX_RULES = (_lane_script_rule, _compiled_input_rule)


def _decide_by_index(
    changed: list[str], root: pathlib.Path, workflows: set[str]
) -> tuple[bool, str]:
    """The half that needs the repository read, split from the name-only half."""
    for rule in INDEX_RULES:
        verdict = rule(changed, root, workflows)
        if verdict is not None:
            return verdict
    return False, f"none of {len(changed)} changed file(s) can reach rustc"


def changed_paths(stream) -> list[str]:
    """The changed-file list, with only the line terminator removed.

    Not `.strip()`. A filename may legally begin or end with a space,
    and trimming it produces a name that matches nothing in the index --
    so the gate would report a confident `false` for a compiled-in file
    that changed.

    Only the empty string is dropped, for the same reason: a name made
    entirely of spaces is a legal path, and discarding it is the same
    error in the same direction. This list comes straight from the files
    API, so there is no padding to filter out anyway.
    """
    names = []
    for line in stream:
        name = line.rstrip("\r\n")
        if name:
            names.append(name)
    return names


def main() -> int:
    root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
    changed = changed_paths(sys.stdin)
    needed, why = decide(changed, root)
    print(f"rust-touched={'true' if needed else 'false'}")
    print(f"reason={why}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write(f"rust-touched={'true' if needed else 'false'}\n")
            handle.write(f"rust-touched-reason={why}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
