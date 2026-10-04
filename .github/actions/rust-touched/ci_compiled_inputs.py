#!/usr/bin/env python3
"""Every non-Rust file the crates compile in, and whether CI would skip it.

`include_str!`, `include_bytes!` and `include!` make a data file part of the
build as surely as a `.rs` file is. Nothing in cargo's own metadata says so --
the path is an argument to a macro, invisible until the crate is expanded --
so a path filter written from the directory layout cannot see it.

maxi-core's ci.yml ignores `**.md` and `docs/**`. `src/lib.rs` compiles in
`docs/EMOJI_GUIDE.md` and `TOOL_ARCHITECTURE.md`, and asserts on their contents
in `github_docs_use_canonical_octopus_and_note_legacy_alias`. Editing either
one therefore skips the entire lane that would have caught the break: the test
guarding those files cannot run when those files are what changed.

This is the index both directions need. A path in it must never be ignored --
that is the false green above. A path NOT in it, and outside every package's
sources, is one a lane can safely skip, which is the only way to stop a
CI-config edit paying for a full Rust build.

Deliberately textual, and deliberately over-inclusive. Resolving these properly
means expanding macros, which means a compiler; every judgement here is instead
resolved towards claiming MORE files are compiled in. A file wrongly listed
costs a CI run that was not needed. A file wrongly omitted costs a green check
on a broken build, which is the failure this exists to prevent.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

# Whitespace only, because comments cannot reach this.
#
# Every pattern in `_includes` runs over `mask_comments(text)`, which replaces
# each ordinary comment -- line, block, and NESTED block, which it counts --
# with spaces of the same length. So by the time this matches, an ordinary
# comment IS whitespace, and an arm for it would be dead.
#
# The one thing masking leaves intact is a doc comment, and this deliberately
# does NOT skip those -- because a doc comment cannot be inside a site. rustc
# rejects one between the tokens of a macro invocation, in either gap; the
# table below `SITE` records the check. So `\s*` is not a compromise here, it
# is exact: everything that can legally appear between `include_str` and its
# `(` is whitespace, or an ordinary comment that masking has already turned
# into whitespace.
#
# This also removes the whole class of problem that a gap pattern invites. The
# arms it used to carry were `//[^\n]*` and `/\*(?:[^*]|\*(?!/))*\*/`, and the
# first could give characters back: a run of slashes then had exponentially
# many ways to split into `//` comments, measured at x2.6 per two added
# slashes -- 26 slashes 6ms, 40 slashes 7.9s, ~60 slashes hours. It was
# reachable because `///...` is a DOC comment and so survives masking, and
# `route` runs this over the .rs files of a FORK pull request. `\s*` cannot
# backtrack into anything.
GAP = r"\s*"

# The macro name (captured, because `include!` alone injects Rust tokens and so
# has to be followed into its target), then the `!` and the open paren. The
# argument is parsed by hand from there -- see `_literal_at`.
SITE = re.compile(r"\binclude(_str|_bytes)?" + GAP + r"!" + GAP + r"\(")

# There is no commented-name fallback, and that is a conclusion from rustc
# rather than an omission.
#
# One existed, to catch a site whose gap held a comment that SURVIVED masking
# -- i.e. a DOC comment, since `mask_comments` replaces every ordinary one with
# spaces. Checked against rustc 1.93.0, that shape does not exist:
#
#   include_str /* d */ !("x")     compiles     ordinary, both gaps
#   include_str! /* d */ ("x")     compiles
#   include_str /** d */ !("x")    syntax error  doc comment, either gap
#   include_str /// d \n !("x")    syntax error
#   include_str! /** d */ ("x")    syntax error
#
# A doc comment cannot sit between the tokens of a macro invocation. So the
# only comments that can be there are ordinary ones, masking turns those into
# spaces, and `GAP` is `\s*` -- `SITE` finds them natively, nested ones
# included. The fallback could therefore only ever fire on something that was
# NOT a macro.
#
# Which is what it did. It matched the English word "include" ending a doc
# comment line followed by the next line's `///` (engine.rs:441, "a config
# include / that is a FIFO"), reported an unresolvable site, and widened the
# gate on EVERY pull request for as long as that sentence was on main.
# Narrowing it then dropped a real shape; a hand-written scan to recover that
# was quadratic on repeated `include /*` in one doc comment, a DoS reachable
# from a fork. Three rounds, all of them defending a case that cannot compile.
#
# Do not reintroduce one without first showing rustc accepts the shape it is
# meant to catch.

# `concat!(env!("OUT_DIR"), ...)` names a file a build script wrote under
# `target/`. It is outside the repository by construction, so no edit can be
# that path -- and the build script that produces it is a `build.rs`, which the
# caller already treats as touching the build. Resolvable to "nothing in this
# repo", not unknown.
# The gap is walked ONCE, by `_after_gap`, and the patterns below match at the
# position it returns. Writing them as `GAP + <pattern>` instead made each one
# re-walk the same whitespace and comments, and put `GAP`'s closing `*`
# immediately before another quantifier -- the shape a backtracking regex is
# built out of, and the one Codacy flags. Matching at a known offset has
# neither problem, and there is one place to reason about the gap.
GAP_RX = re.compile(GAP)
OUT_DIR_ARG = re.compile(r"concat!\s*\(\s*env!\s*\(\s*\"OUT_DIR\"")

# A raw string carries no escapes and ends only at a quote followed by the same
# number of hashes it opened with, so `r#"a"b.txt"#` is one path, not `a`.
RAW_OPEN = re.compile(r"r(#*)\"")
# A cooked string, taken only when it holds no backslash and no newline.
# `"..\\x.txt"` is Rust source for `..\x.txt`; reading the source form as a path
# indexes a file that does not exist and leaves the real one skippable.
# Decoding Rust's escape grammar here would be a second, unverifiable parser,
# so anything with an escape in it fails to match and is reported unresolved
# instead -- the direction that costs minutes rather than the premise.
PLAIN_COOKED = re.compile(r"\"([^\"\\\n]*)\"")


def _after_gap(text: str, pos: int) -> int:
    """Index of the first character at or after `pos` that is not gap.

    `GAP` can match empty, so this always succeeds and the result is never
    behind `pos`.
    """
    return GAP_RX.match(text, pos).end()


def _literal_at(text: str, pos: int) -> str | None:
    """The path a literal argument at `pos` names, or None if not readable.

    None covers both "this is not a literal" (`concat!(...)`) and "this is a
    literal this cannot decode" (any escape). The caller treats them the same
    way, because both mean the same thing: the file this site depends on is
    unknown, so nothing may be skipped on the strength of it.
    """
    pos = _after_gap(text, pos)
    raw = RAW_OPEN.match(text, pos)
    if raw:
        close = '"' + raw.group(1)
        end = text.find(close, raw.end())
        return None if end < 0 else text[raw.end():end]
    plain = PLAIN_COOKED.match(text, pos)
    return plain.group(1) if plain else None


def _vendored(rel: pathlib.PurePath) -> bool:
    """Build output and git internals are not sources anyone edits."""
    return bool(rel.parts) and rel.parts[0] in {"target", ".git"}


def _skip_block_comment(text: str, i: int) -> int:
    """Index just past a block comment starting at `i`, honouring nesting.

    Rust block comments NEST -- `/*a/*b*/c*/` is one comment -- which is why
    this counts depth instead of finding the first `*/`. rustc lexes `/*` as a
    comment opener in every context, so there is no "division by a dereference"
    case to exclude: `a/*b` is an unterminated-block-comment ERROR in rustc,
    and the division is written `a / *b`, whose space this never sees.
    """
    depth, n = 1, len(text)
    i += 2
    while i < n and depth:
        if text.startswith("/*", i):
            depth += 1
            i += 2
        elif text.startswith("*/", i):
            depth -= 1
            i += 2
        else:
            i += 1
    return i


def _skip_string(text: str, i: int) -> int:
    """Index just past an ordinary string literal starting at its quote.

    Char literals are NOT tracked, and do not need to be. `'"'` puts a quote
    where this reads the start of a string, so the cursor can run to the wrong
    place -- but a cursor error here cannot hide an include, because MASKING
    only ever happens inside a recognised comment and this function masks
    nothing. Losing string state can leave a comment unmasked, which counts a
    macro written in prose: over-inclusion, one wasted CI run.

    That is the direction this whole module is required to fail in, so the
    handling stops here rather than growing a char-literal branch that would
    add a lifetime-vs-literal ambiguity (`&'a str`) to buy nothing. Verified
    across seven arrangements of char literals with comments, raw strings and
    nested quotes: none hides an include; the one that changes anything
    over-includes.
    """
    n = len(text)
    i += 1
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return n


def _skip_raw_string(text: str, i: int) -> int | None:
    """Index just past a raw string at `i`, or None if `i` does not start one.

    A raw string carries its own hash count, so `r#"a"#` ends only at a quote
    followed by that many hashes -- a plain search for `"` closes it early and
    leaves the rest of the file being read as code.
    """
    n = len(text)
    j = i + 1
    hashes = 0
    while j < n and text[j] == "#":
        hashes += 1
        j += 1
    if j >= n or text[j] != '"':
        return None
    closer = '"' + "#" * hashes
    end = text.find(closer, j + 1)
    return n if end == -1 else end + len(closer)


def _is_doc_comment(text: str, i: int) -> bool:
    """Whether a comment at `i` is a DOC comment, in either form.

    rustdoc compiles the examples inside `///`, `//!`, `/**` and `/*!`, so an
    `include_str!` in any of them is a real dependency and must stay visible.
    Only the line forms were exempted at first, which left a doctest inside a
    `/** ... */` block masked -- the gate could then skip the lanes after the
    asset it names changed.

    `/**/` needs no special case. It is self-contained and empty, so whether it
    is masked or left alone cannot change what the index finds -- an earlier
    version excluded it explicitly and the test for that exclusion passed with
    the exclusion deleted, which is the whole reason it is gone.
    """
    if text.startswith("///", i) or text.startswith("//!", i):
        return True
    return text.startswith("/*!", i) or text.startswith("/**", i)


def mask_comments(text: str) -> str:
    """`text` with every non-doc comment replaced by spaces, same length.

    Masking rather than deleting keeps every offset, so line numbers and the
    subtraction in `_includes` stay exact and no caller remaps anything.

    Doc comments are left intact deliberately -- see `_is_doc_comment`. The
    cost is over-inclusion: a ```ignore example is indexed as though it
    compiled, which spends a CI run. The other direction spends a green check
    on a broken doctest.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and text.startswith("/*", i):
            end = _skip_block_comment(text, i)
            if not _is_doc_comment(text, i):
                for j in range(i, min(end, n)):
                    if out[j] != "\n":
                        out[j] = " "
            i = end
            continue
        if ch == "/" and text.startswith("//", i):
            doc = _is_doc_comment(text, i)
            while i < n and text[i] != "\n":
                if not doc:
                    out[i] = " "
                i += 1
            continue
        if ch == "r":
            end = _skip_raw_string(text, i)
            if end is not None:
                i = end
                continue
        if ch == '"':
            i = _skip_string(text, i)
            continue
        i += 1
    return "".join(out)


def _includes(text: str) -> tuple[list[tuple[str, str]], list[int]]:
    """(macro suffix, literal path) pairs, and the offsets of unreadable sites.

    `unresolved` is computed by SUBTRACTION -- a site whose argument did not
    parse as a literal -- rather than by a second "not a literal" pattern. The
    obvious second pattern is wrong in a way that is easy to ship: with
    `\\s*(?!r?\\#*")` the `\\s*` backtracks to zero width, the lookahead then
    sees the newline instead of the quote and trivially succeeds, and every
    argument written on its own line is reported as non-literal. That fired on
    two real sites here and would have pinned the gate to "always widen", which
    looks like caution and is actually a silent no-op.
    """
    # Every pattern below runs over the MASKED text, so a macro invocation
    # hidden behind a nested block comment cannot be missed and a `include_str!`
    # written inside a comment cannot be counted.
    text = mask_comments(text)
    args: list[tuple[str, str]] = []
    unreadable: list[int] = []
    for site in SITE.finditer(text):
        if OUT_DIR_ARG.match(text, _after_gap(text, site.end())):
            continue
        arg = _literal_at(text, site.end())
        if arg is None:
            unreadable.append(site.start())
        elif arg:
            args.append((site.group(1) or "", arg))
    return args, unreadable


def compiled_inputs(root: pathlib.Path) -> tuple[set[str], list[str]]:
    """Repo-relative paths compiled in, and the sites that could not be read.

    The second half matters as much as the first. A `include_str!(concat!(...))`
    resolves to a path this cannot compute, and treating that as "no file" is
    exactly the omission that produces a false green -- so it is returned for
    the caller to fail on rather than dropped.

    `include!` is followed into its target, whatever the target is named. It
    injects Rust tokens, so a `gen/table.in` reached that way is Rust source
    that the `*.rs` walk below would otherwise never read -- and its own
    `include_str!` is a real dependency of the build.
    """
    root = root.resolve()
    found: set[str] = set()
    unresolved: list[str] = []
    queue = [path for path in root.rglob("*.rs") if not _vendored(path.relative_to(root))]
    scanned: set[pathlib.Path] = set()
    while queue:
        path = queue.pop()
        # A cycle of `include!`s is a compile error, not something to hang on.
        if path in scanned:
            continue
        scanned.add(path)
        rel = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Unreadable source is not evidence of absence.
            unresolved.append(f"{rel}: unreadable")
            continue
        args, unreadable = _includes(text)
        for offset in unreadable:
            line = text.count("\n", 0, offset) + 1
            unresolved.append(f"{rel}:{line}: non-literal include")
        for macro, arg in args:
            # Relative to the FILE, which is what rustc does -- not to the
            # crate root and not to the workspace.
            target = (path.parent / arg).resolve()
            try:
                found.add(str(target.relative_to(root)))
            except ValueError:
                # Outside the repo: cannot be affected by a change to it.
                continue
            if macro == "" and target.is_file():
                queue.append(target)
    return found, unresolved


def main() -> int:
    root = pathlib.Path(os.environ.get("REPO_ROOT", ".")).resolve()
    found, unresolved = compiled_inputs(root)
    for rel in sorted(found):
        print(rel)
    for note in unresolved:
        print(f"unresolved: {note}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
