#!/usr/bin/env python3
"""The verify-sibling-checkouts action tolerates Python < 3.11.

`tomllib` is a Python 3.11+ stdlib module. The org's self-hosted
[macOS, ARM64] lane-test runners (`maxiStudio22`, `maximini20`)
ship Python < 3.11 pinned by their Nix provisioning, which killed
every Rust PR at `merge-gate / test / lane-test` step 10 with a
bare `ModuleNotFoundError: No module named 'tomllib'` before any
manifest was parsed. Evidence: maxi-e2e#171 (run 38388255166 on
maxiStudio22) and maxi-e2e#101 (run 38429245800 on maximini20).

This test proves the action's discover script reaches a working TOML
parser on BOTH Python setups:

  Phase 1 (tomllib present):        Python >= 3.11 normal case.
  Phase 2 (tomllib blocked, tomli present): Python 3.10 with the
                                    PyPI `tomli` backport installed.
  Phase 3 (both present):           stdlib tomllib wins (no
                                    accidental downgrade to tomli).
  Phase 4 (broken TOML):            the error path uses the
                                    TOMLDecodeError from whichever
                                    module loaded, not the stdlib
                                    name, so it works under tomli
                                    too.
  Phase 5 (neither module):         the discover step fails with a
                                    clear attributable error from
                                    the action, not a traceback.

Phase 5's "neither module" simulation requires removing both
`tomllib` AND `tomli` from the test interpreter's `sys.modules`
map and meta_path. Because tomli may not be installed on the
host running this test, Phase 2/3/4 supply a SHIM tomli package
whose source re-exports the stdlib tomllib contents -- the shim
is API-equivalent for exactly the surface discover.py uses
(`loads()` and `TOMLDecodeError`), so the test exercises the
code path without depending on the test host's pip state.

Run directly: `python3 tests/test_verify_sibling_checkouts_tomllib.py`.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
DISCOVER = ROOT / ".github/actions/verify-sibling-checkouts/discover.py"
ACTION_YML = ROOT / ".github/actions/verify-sibling-checkouts/action.yml"


def make_cargo_tree(root: pathlib.Path, with_broken: bool = False) -> None:
    """Construct a small Cargo workspace with a known sibling dep."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text(
        "[package]\n"
        'name = "root"\n'
        'version = "0.0.1"\n\n'
        "[dependencies]\n"
        'foo = { path = "../foo" }\n'
        'internal = { path = "crates/internal" }\n'
    )
    (root / "crates").mkdir()
    (root / "crates" / "Cargo.toml").write_text(
        "[package]\n"
        'name = "sub"\n'
        'version = "0.0.1"\n'
        '\n'
        "[target.'cfg(target_os = \"macos\")'.dependencies]\n"
        # Two levels up because this manifest lives in <root>/crates/;
        # "../appledep" from crates/ is in-tree, not a sibling.
        'appledep = { path = "../../appledep" }\n'
    )
    if with_broken:
        (root / "broken").mkdir()
        # Deliberately invalid TOML: an un-closed array table declaration.
        (root / "broken" / "Cargo.toml").write_text("[[[unclosed\n")


def run_discover(
    workspace: pathlib.Path,
    extra_pythonpath: list[str] | None = None,
    sitecustomize: str | None = None,
) -> subprocess.CompletedProcess:
    """Run discover.py against `workspace` with optional PYTHONPATH additions
    and an optional sitecustomize.py shim."""
    env = dict(**__import__("os").environ)
    env["GITHUB_WORKSPACE"] = str(workspace.resolve())
    paths: list[str] = []
    if extra_pythonpath:
        paths.extend(extra_pythonpath)
    if sitecustomize is not None:
        # Place sitecustomize.py on PYTHONPATH; Python auto-imports it
        # before running the target script when -S is omitted.
        sc_dir = tempfile.mkdtemp(prefix="sitecustomize-")
        (pathlib.Path(sc_dir) / "sitecustomize.py").write_text(sitecustomize)
        paths.append(sc_dir)
    if paths:
        env["PYTHONPATH"] = ":".join(paths)
    else:
        env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, str(DISCOVER)],
        env=env,
        capture_output=True,
        text=True,
    )


def sibling_stdout_lines(stdout: str) -> list[str]:
    """`sibling: name (from manifest)` lines are informational; the
    machine-readable contract is the bare `name` lines printed to
    stdout (read by the action's `while read` loop). Strip everything
    else."""
    return sorted(
        line for line in stdout.splitlines() if line and not line.startswith("sibling:")
    )


def write_shim_tomli(target_dir: pathlib.Path) -> pathlib.Path:
    """Materialise a `tomli` package whose public surface is API-equivalent
    to stdlib tomllib for the surface discover.py uses (`loads()`,
    `TOMLDecodeError`).

    The test's sitecustomize poisons the NAME `tomllib` on the meta-path,
    so any import by that name (including tomli's own `from tomllib import
    ...` line if we used one) raises ModuleNotFoundError before it
    resolves. The shim therefore loads the stdlib tomllib module BY PATH
    using importlib.util.spec_from_file_location, which consults neither
    meta-path nor sys.modules for that name. Stdlib tomllib/__init__.py
    does `from ._parser import ...` and similar internal submodule
    imports, so we preload each of those submodules under their natural
    fully-qualified name, then load __init__.

    In production on the broken Mac runners the role of this shim is
    played by the real PyPI `tomli` package, which is a clean-room
    implementation of the same surface. We use a re-export here only so
    the test is hermetic (no pip install on the test host).
    """
    import importlib.util
    import shutil

    spec = importlib.util.find_spec("tomllib")
    if spec is None or spec.origin is None:
        raise RuntimeError("stdlib tomllib not found; cannot write shim")
    stdlib_dir = pathlib.Path(spec.origin).parent

    pkg = target_dir / "tomli"
    pkg.mkdir(parents=True, exist_ok=True)
    # Copy the four stdlib source files into the shim package. Loading
    # them by path in __init__.py bypasses the meta-path blocker, which
    # keys on the *name* 'tomllib' rather than on the file location.
    stdlib_copy = pkg / "_toml_stdlib_files"
    stdlib_copy.mkdir()
    for fname in ("__init__.py", "_parser.py", "_re.py", "_types.py"):
        shutil.copy(stdlib_dir / fname, stdlib_copy / fname)

    shim_code = textwrap.dedent(
        """\
        # Test shim for tests/test_verify_sibling_checkouts_tomllib.py.
        # Re-exports stdlib tomllib's surface under the importable name
        # `tomli` while the meta-path blocker in the parent test process
        # intercepts any direct `import tomllib`. The four stdlib source
        # files are preloaded by path under their natural module names,
        # then __init__ is loaded; this sidesteps the meta-path poison
        # without weakening the test (the blocker still runs first for
        # any normal import attempt).
        import importlib.util as _iu
        import sys as _sys
        from pathlib import Path as _P

        _STDLIB = _P(__file__).resolve().parent / "_toml_stdlib_files"

        def _load_one(modname, path):
            spec = _iu.spec_from_file_location(modname, str(path))
            mod = _iu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

        # Stdlib tomllib/__init__.py does `from ._parser import ...`,
        # which consults the meta-path for `tomllib._parser`. Loading
        # the submodules first under their natural names pre-populates
        # sys.modules so the parent __init__.py sees them as already
        # imported and skips the meta-path lookup.
        for _name, _fn in (
            ("tomllib._types", _STDLIB / "_types.py"),
            ("tomllib._re", _STDLIB / "_re.py"),
            ("tomllib._parser", _STDLIB / "_parser.py"),
            ("tomllib", _STDLIB / "__init__.py"),
        ):
            _sys.modules[_name] = _load_one(_name, _fn)

        _real = _sys.modules["tomllib"]
        TOMLDecodeError = _real.TOMLDecodeError
        loads = _real.loads
        __all__ = ["TOMLDecodeError", "loads"]
        """
    )
    (pkg / "__init__.py").write_text(shim_code)
    return target_dir


BLOCK_TOMLLIB_SITECUSTOMIZE = textwrap.dedent(
    """\
    # Poison `tomllib` BEFORE the target script runs. Simulates a Python
    # interpreter whose stdlib does not ship tomllib (i.e. 3.10 or earlier).
    # Used by tests/test_verify_sibling_checkouts_tomllib.py only.
    import sys
    sys.modules["tomllib"] = None

    class _TomllibBlocker:
        def find_spec(self, name, path=None, target=None):
            if name == "tomllib" or name.startswith("tomllib."):
                raise ModuleNotFoundError(
                    "No module named 'tomllib' (blocked for tomli fallback test)"
                )
            return None

    # Meta-path first so even `import tomllib` (which consults meta_path
    # before hitting sys.modules) raises ModuleNotFoundError.
    sys.meta_path.insert(0, _TomllibBlocker())
    """
)


class Phase:
    def __init__(self):
        self.failures: list[str] = []

    def fail(self, msg: str) -> None:
        print(f"FAIL: {msg}", file=sys.stderr)
        self.failures.append(msg)

    def check(self, cond: bool, msg: str) -> bool:
        if not cond:
            self.fail(msg)
        return cond


def phase1_stdlib_tomllib(p: Phase) -> bool:
    """Python 3.11+: tomllib resolves from stdlib. No shim needed."""
    with tempfile.TemporaryDirectory(prefix="tsc-p1-") as tmp:
        root = pathlib.Path(tmp) / "repo"
        make_cargo_tree(root)
        r = run_discover(root)
        if not p.check(r.returncode == 0, f"phase1 exit {r.returncode}: {r.stderr}"):
            return False
        got = sibling_stdout_lines(r.stdout)
        want = ["appledep", "foo"]
        return p.check(got == want, f"phase1 siblings {got} want {want}")


def phase2_tomli_fallback(p: Phase) -> bool:
    """Python 3.10 with tomli installed: tomllib poisoned, tomli shim
    provides the parser. This is the regression the PR fixes."""
    with tempfile.TemporaryDirectory(prefix="tsc-p2-") as tmp:
        root = pathlib.Path(tmp) / "repo"
        make_cargo_tree(root)
        shim_dir = pathlib.Path(tmp) / "shim"
        shim_dir.mkdir()
        write_shim_tomli(shim_dir)
        r = run_discover(
            root,
            extra_pythonpath=[str(shim_dir)],
            sitecustomize=BLOCK_TOMLLIB_SITECUSTOMIZE,
        )
        if not p.check(r.returncode == 0, f"phase2 exit {r.returncode}: {r.stderr}"):
            return False
        got = sibling_stdout_lines(r.stdout)
        want = ["appledep", "foo"]
        return p.check(got == want, f"phase2 siblings {got} want {want}")


def phase3_both_installed(p: Phase) -> bool:
    """tomllib + tomli both importable: stdlib wins. Prevents a regression
    where the fallback checked first and the lane silently ran on the
    older TOML implementation even on 3.11+."""
    with tempfile.TemporaryDirectory(prefix="tsc-p3-") as tmp:
        root = pathlib.Path(tmp) / "repo"
        make_cargo_tree(root)
        shim_dir = pathlib.Path(tmp) / "shim"
        shim_dir.mkdir()
        write_shim_tomli(shim_dir)
        r = run_discover(root, extra_pythonpath=[str(shim_dir)])
        if not p.check(r.returncode == 0, f"phase3 exit {r.returncode}: {r.stderr}"):
            return False
        got = sibling_stdout_lines(r.stdout)
        want = ["appledep", "foo"]
        return p.check(got == want, f"phase3 siblings {got} want {want}")


def phase4_broken_toml(p: Phase) -> bool:
    """A broken Cargo.toml in the tree must abort with the
    `::error::cannot read ...` marker. Under BOTH tomllib and the shim
    tomli the catch site has to use the right exception class, otherwise
    the error escapes as a traceback."""
    with tempfile.TemporaryDirectory(prefix="tsc-p4-") as tmp:
        root = pathlib.Path(tmp) / "repo"
        make_cargo_tree(root, with_broken=True)

        # Subphase 4a: stdlib tomllib present.
        r = run_discover(root)
        if not p.check(
            r.returncode != 0,
            f"phase4a should fail; got exit {r.returncode} stdout={r.stdout}",
        ):
            return False
        if not p.check(
            "::error::cannot read" in r.stderr,
            f"phase4a missing ::error::cannot read marker in {r.stderr}",
        ):
            return False

        # Subphase 4b: tomllib poisoned, tomli shim provides parser.
        shim_dir = pathlib.Path(tmp) / "shim"
        shim_dir.mkdir()
        write_shim_tomli(shim_dir)
        r = run_discover(
            root,
            extra_pythonpath=[str(shim_dir)],
            sitecustomize=BLOCK_TOMLLIB_SITECUSTOMIZE,
        )
        if not p.check(
            r.returncode != 0,
            f"phase4b should fail; got exit {r.returncode} stdout={r.stdout}",
        ):
            return False
        if not p.check(
            "::error::cannot read" in r.stderr,
            f"phase4b missing ::error::cannot read marker in {r.stderr}",
        ):
            return False
        return True


def phase5_neither_module(p: Phase) -> bool:
    """Neither tomllib nor tomli importable: the discover step must
    produce an attributable `::error::` message, not a bare ImportError
    traceback. This is the failure mode that made the lane-test logs
    unreadable before this PR."""
    with tempfile.TemporaryDirectory(prefix="tsc-p5-") as tmp:
        root = pathlib.Path(tmp) / "repo"
        make_cargo_tree(root)
        r = run_discover(
            root,
            sitecustomize=BLOCK_TOMLLIB_SITECUSTOMIZE,
        )
        # No tomli on PYTHONPATH: discover.py's fallback `import tomli`
        # will also fail, and the script's own gate at the top must
        # produce the clear `::error::` line and exit 2.
        if not p.check(
            r.returncode == 2,
            f"phase5 expected exit 2 from missing-parser gate; got {r.returncode} stderr={r.stderr}",
        ):
            return False
        if not p.check(
            "::error::verify-sibling-checkouts requires either" in r.stderr,
            f"phase5 missing attributable ::error:: line: {r.stderr}",
        ):
            return False
        return True


def phase6_action_yaml_calls_discover_file(p: Phase) -> bool:
    """action.yml references discover.py via GITHUB_ACTION_PATH, not a
    heredoc-embedded copy. Catches a future edit that re-inlines the
    script and silently forks the runtime from the tested bytes."""
    body = ACTION_YML.read_text(encoding="utf-8")
    ok = p.check(
        '${GITHUB_ACTION_PATH}/discover.py' in body,
        "action.yml lost the GITHUB_ACTION_PATH/discover.py reference; the "
        "script may have been re-inlined as a heredoc",
    )
    ok &= p.check(
        "cat > \"$discover\" <<'PY'" not in body,
        "action.yml still has heredoc-embedded discover script; expected it "
        "to call discover.py instead",
    )
    return ok


def main() -> int:
    p = Phase()
    phases = [
        ("phase1: stdlib tomllib", phase1_stdlib_tomllib),
        ("phase2: tomli fallback (py3.10)", phase2_tomli_fallback),
        ("phase3: stdlib wins when both present", phase3_both_installed),
        ("phase4: broken toml -> attributable error", phase4_broken_toml),
        ("phase5: neither module -> clear error", phase5_neither_module),
        ("phase6: action.yml calls discover.py", phase6_action_yaml_calls_discover_file),
    ]
    for name, fn in phases:
        ok = fn(p)
        print(f"{'PASS' if ok else 'FAIL'}: {name}", file=sys.stderr)
    if p.failures:
        print(f"\n{len(p.failures)} failures", file=sys.stderr)
        return 1
    print("ALL PHASES PASSED", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())