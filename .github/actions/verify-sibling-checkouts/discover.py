#!/usr/bin/env python3
"""Discover Cargo `path = "../<repo>"` dependencies that resolve outside this
checkout.

Read by .github/actions/verify-sibling-checkouts/action.yml. Lifted out of
the action's heredoc so it is exercisable directly: tests/test_verify_
sibling_checkouts_tomllib.py runs this file under a stripped module table
to prove the Python 3.10 / tomli fallback path works end-to-end, not just
that the imports resolve at module load.

Why a tomli fallback exists
----------------------------

`tomllib` is a Python 3.11+ stdlib module. The org's macOS self-hosted
runners (`maxiStudio22`, `maximini20` confirmed 2026-10-13) ship a
`python3` that is older than 3.11, so a bare `import tomllib` raises
`ModuleNotFoundError` before any manifest is parsed and the lane-test
lane fails every PR on `maxi-tools/maxi-e2e` and any other gated Rust
repo at step 10 (maxi-config#286 / the lane-test reuse).

`toli` (PyPI) is the official backport of `tomllib` for Python 3.6+ and
exposes the same `loads()` / `TOMLDecodeError` surface. We try it as a
fallback before failing the lane, so the lane works on every Python 3.10+
runner that has either stdlib `tomllib` (3.11+) OR the `tomli` PyPI
package installed.

This file deliberately has no other dependencies so it can be vendored
into the action with no further install step.
"""

from __future__ import annotations

import os
import pathlib
import sys

# `tomllib` is the stdlib name on Python 3.11+; on 3.10 and below we fall
# back to the `tomli` PyPI package (the official backport), exposing it
# under the local name `tomllib_mod` so the rest of the script never
# branches on which module was found. The exception classes are NOT
# identical between the two modules (`tomllib.TOMLDecodeError` and
# `tomli.TOMLDecodeError` are distinct classes), so we catch both
# explicitly below.
try:
    import tomllib as tomllib_mod  # type: ignore[import-not-found]
except ModuleNotFoundError:
    try:
        import tomli as tomllib_mod  # type: ignore[no-redef]
    except ModuleNotFoundError:
        # Surface an attributable error from the action's bash wrapper.
        # The wrapper checks for this marker string verbatim.
        print(
            "::error::verify-sibling-checkouts requires either Python "
            ">= 3.11 (stdlib `tomllib`) or the `tomli` PyPI package "
            "installed on the runner; neither is importable.",
            file=sys.stderr,
        )
        sys.exit(2)

TOML_DECODE_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    tomllib_mod.TOMLDecodeError,
)


def harvest_dep_table(table):
    """[dependencies] / [dev-dependencies] / [build-dependencies]
    / [workspace.dependencies] -- a name -> dep-spec table. Each
    spec is either a string (version) or a table that may carry
    `path`. Skip string specs."""
    if not isinstance(table, dict):
        return
    for v in table.values():
        if isinstance(v, dict) and isinstance(v.get("path"), str):
            yield v["path"]


def harvest_patch_table(table):
    """[patch.<registry>] -- registry name -> crate-name -> spec."""
    if not isinstance(table, dict):
        return
    for reg in table.values():
        if not isinstance(reg, dict):
            continue
        for v in reg.values():
            if isinstance(v, dict) and isinstance(v.get("path"), str):
                yield v["path"]


def harvest_replace_table(table):
    """[replace] -- crate-name -> spec (single map, not registry-
    scoped). Same shape as a dependency spec."""
    if not isinstance(table, dict):
        return
    for v in table.values():
        if isinstance(v, dict) and isinstance(v.get("path"), str):
            yield v["path"]


def discover(root: pathlib.Path) -> dict[str, set[str]]:
    """Walk `root` for Cargo.toml files and return the set of sibling
    repositories named by `path = "../..."` entries that escape the
    checkout root.

    Returns a dict mapping sibling repo name -> set of relative
    manifests that declared the dependency. A failure that should
    abort the lane (escape above the parent, broken TOML, ...) raises
    `SystemExit(1)` after writing a `::error::` line; this matches
    the in-place heredoc behaviour so the action's caller logic does
    not change."""
    skip = {
        ".git", "target", ".worktrees", ".maxi-worktrees",
        "node_modules", "vendor",
    }
    siblings: dict[str, set[str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
        if "Cargo.toml" not in filenames:
            continue
        manifest = pathlib.Path(dirpath) / "Cargo.toml"
        try:
            doc = tomllib_mod.loads(
                manifest.read_text(encoding="utf-8", errors="replace")
            )
        except TOML_DECODE_ERRORS as exc:
            print(f"::error::cannot read {manifest}: {exc}", file=sys.stderr)
            sys.exit(1)
        paths = []
        # Top-level dependency tables. tomllib parses nested tables as
        # nested dicts, so `[workspace]` and `[workspace.dependencies]`
        # both appear under doc["workspace"]; harvest_dep_table is
        # called on both shapes directly.
        for key in ("dependencies", "dev-dependencies", "build-dependencies"):
            paths.extend(harvest_dep_table(doc.get(key)))
        ws = doc.get("workspace")
        if isinstance(ws, dict):
            paths.extend(harvest_dep_table(ws.get("dependencies")))
        # Target-specific dependency tables. `[target.'cfg(...)'.dependencies]`
        # is parsed as doc["target"][<triple-quoted key>]["dependencies"],
        # so walk the target map and harvest every {dependencies, dev-
        # dependencies, build-dependencies} table under it. These are
        # the tables Cargo consults when the matching cfg is active --
        # e.g. Apple lanes resolve `[target.'cfg(target_os = "macos")'.
        # dependencies]`, so missing or stale siblings there would
        # otherwise surface as cargo-time errors on the lane the issue
        # documents.
        target = doc.get("target")
        if isinstance(target, dict):
            for t in target.values():
                if not isinstance(t, dict):
                    continue
                for key in ("dependencies", "dev-dependencies", "build-dependencies"):
                    paths.extend(harvest_dep_table(t.get(key)))
        # `[patch.<registry>]` may live at doc["patch"] (single
        # registry block) or under doc["workspace"]["patch"] (the
        # workspace-wide patch). Either way it's a registry map.
        for patch_root in (doc.get("patch"), (ws or {}).get("patch")):
            paths.extend(harvest_patch_table(patch_root))
        # Deprecated `[replace]` -- Cargo still honours it on 1.95.
        paths.extend(harvest_replace_table(doc.get("replace")))
        for raw in paths:
            # `os.path.normpath` first, then re-check for `..` in the
            # segment. A `path = "./../foo/crate"` declaration would
            # be skipped by a raw `startswith("..")` check even though
            # it normpath()s to `../foo/crate`; without normalisation
            # the lane retains both original failure modes (missing
            # manifest, false-positive green against a stale sibling).
            if not os.path.normpath(raw).split(os.sep)[0].startswith(".."):
                # Pure intra-tree path, e.g. `path = "crates/foo"` or
                # `path = "./crates/foo"`. Cargo resolves it under
                # this checkout and the lane already handles it.
                continue
            # Lexical normalisation. `Path.resolve()` follows symlinks
            # on disk, which means a leftover sibling at ../waterui
            # that points at ../waterui-old would resolve to the wrong
            # repository -- the action would clone the wrong tree and
            # leave a stale symlink in place for cargo to follow. Decide
            # the sibling from the manifest, not from the runner.
            resolved = pathlib.Path(os.path.normpath(manifest.parent / raw))
            try:
                resolved.relative_to(root)
                continue
            except ValueError:
                pass
            # root/../<name> normpath()s away the `..`, so the sibling
            # is the first path component that departs from the
            # checkout root's own parts.
            root_parts = root.parts
            parts = resolved.parts
            if len(parts) < len(root_parts):
                print(
                    f"::error::{manifest.relative_to(root)} path {raw} "
                    f"resolves to {resolved}, which is the checkout's "
                    "parent or above; a Cargo path dependency must name "
                    "a sibling of this checkout, not the checkout itself",
                    file=sys.stderr,
                )
                sys.exit(1)
            if parts[: len(root_parts)] == root_parts:
                continue
            if parts[: len(root_parts) - 1] != root_parts[:-1]:
                print(
                    f"::error::{manifest.relative_to(root)} path {raw} "
                    f"escapes to {resolved}, which is not a direct sibling "
                    "of this checkout",
                    file=sys.stderr,
                )
                sys.exit(1)
            name = parts[len(root_parts) - 1]
            siblings.setdefault(name, set()).add(str(manifest.relative_to(root)))
    return siblings


def main() -> int:
    # The action sets GITHUB_WORKSPACE; the script falls back to CWD for
    # local debugging.
    workspace = os.environ.get("GITHUB_WORKSPACE")
    if workspace:
        root = pathlib.Path(workspace).resolve()
    else:
        root = pathlib.Path(os.getcwd()).resolve()
    siblings = discover(root)
    for name in sorted(siblings):
        where = ", ".join(sorted(siblings[name]))
        print(f"sibling: {name} (from {where})", file=sys.stderr)
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())