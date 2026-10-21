#!/usr/bin/env python3
"""Deduplicate bot review threads across pushes and within the same round.

Two duplicate shapes accumulate on PRs in this org:

1. Cross-push / same-finding bot re-posts (measured on maxi-core#4897,
   2026-10-21: 262 qltysh threads, 247 unresolved across 16 pushes, 0
   outdated, but only 31 distinct `(path, finding-text)` keys -- e.g. x48
   on `tests/test_quality_coverage_workflow.py` with 3 copies per commit).
   `qltysh` re-posts every finding on every push and appends a commit/run-
   specific `<a href="https://qlty.sh/...">` link to the comment body, so
   exact-body comparison misses and single-round multi-bot clustering skips
   single-author components (`len(authors) < 2`) and already-resolved older
   threads (`isResolved == True`).

2. Same-round multi-bot co-location (measured on maxi-core 2026-09-16..18:
   55 bot comments across 9 PRs, 8 sites with two or more bots in the same
   8-line window). When two or more review bots flag the same code in the
   same round, every thread still has to be replied to and resolved before
   review-gate goes green.

This script handles both shapes.

WHAT IT TOUCHES
  * Resolves review threads that THIS script previously resolved, on re-run.
  * Resolves unresolved bot review threads whose normalised finding key
    `(author, path, finding text with HTML links/URLs/commit SHAs stripped,
    line if not outdated)` matches an OLDER thread on the same PR (keeping
    the oldest open, or resolving the newer re-post too when the oldest was
    already resolved by a human/agent decision).
  * Resolves bot review threads whose author is in BOT_LOGINS, when they
    share a (path, +-4 line window) with another bot thread from a distinct
    bot login in the same window.
  * Posts one reply on each thread it resolves, citing the kept thread.

WHAT IT DOES NOT TOUCH
  * Human-authored threads. Filtered before clustering.
  * The last remaining open copy of an undecided finding key.
  * File-level threads (no line number and not outdated). Kept -- a
    file-level flag is a different conversation even when two bots raise it,
    because neither points at code.
  * Threads whose author is not in BOT_LOGINS. The list is explicit on
    purpose: a bot not on it stays alone.

Inputs (all optional; sensible defaults for the GitHub Actions environment):

  --owner / --repo / --pr      PR coordinates (default: $GITHUB_REPOSITORY
                               owner/repo and $PR_NUMBER or
                               $GITHUB_EVENT_PULL_REQUEST_NUMBER)
  --token                      GitHub token (default: $GITHUB_TOKEN or
                               $GH_TOKEN)
  --window                     Line window for clustering (default 4 -- +-4)
  --bot                        Additional bot login (repeatable). Merged with
                               BOT_LOGINS; useful for tests and forks.
  --apply                      Actually post replies and resolve threads.
                               Default is dry-run: print the plan and exit 0.
                               Dry-run NEVER writes anything, which is the
                               safe direction under --apply semantics.

Output (stdout): a small JSON report describing what was found, what was
done, and what was kept. Exit 0 always when the PR could be read; non-zero
when the inputs are unusable.

WHY A SENTINEL. Two attempts on the same PR have to converge -- the second
one must not re-post work the first one already did. State alone does not
pin this: a thread can be left "resolved by us" and a third party can
re-open it, or the reply can succeed while the resolve fails (a network
blip in between). The reply carries a hidden HTML comment
`<!-- maxi-config:dedupe -->`, so a re-run that finds a thread with our
sentinel in its comment body knows the REPLY half is done. The thread's
resolved state is the other half: sentinel + unresolved is a previous
run whose resolve mutation failed, so the re-run retries the resolve
without re-posting the reply; sentinel + resolved is a fully-settled
thread and is skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from typing import Any


#: Bots active in the org whose threads we deduplicate.
#:
#: Six, not three. The original three (codacy-production, coderabbitai,
#: maxi-reviewer) cover the well-known lanes; cubic-dev-ai (the
#: `cubic . AI code reviewer` GitHub App), chatgpt-codex-connector (the
#: ChatGPT Codex Connector GitHub App), and qltysh (Qlty Cloud inline review
#: comments, which re-post every `mode = "comment"` plugin finding on every
#: push) post prolifically on this org's PRs.
BOT_LOGINS: tuple[str, ...] = (
    "codacy-production",
    "coderabbitai",
    "maxi-reviewer",
    "cubic-dev-ai",
    "chatgpt-codex-connector",
    "qltysh",
)

#: Default +-window for clustering. 4 means two threads whose lines differ
#: by up to 8 collapse together, which matches the spec's wording.
WINDOW: int = 4

#: Hidden marker we leave in every reply we post. Hidden to humans, visible
#: to a re-run. The leading space and `<!--` are deliberate: GitHub's
#: comment renderer collapses HTML comments, so the reader sees only the
#: human-readable message; a re-run greps the raw body and finds the
#: marker.
SENTINEL = "<!-- maxi-config:dedupe -->"

#: Cap on threads we will process in one run. Raised from 100 to 500 so a
#: multi-push PR with accumulated qltysh re-posts (e.g. maxi-core#4897: 262
#: qltysh threads, 247 unresolved across 31 distinct findings -> 216
#: duplicates) can be drained in a single run instead of bailing out.
MAX_RESOLVE_PER_RUN = 500

#: GitHub GraphQL `Query.nodes(ids:)` accepts at most 100 IDs per call.
_GRAPHQL_NODES_BATCH_SIZE = 100

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_HTML_LINK_RE = re.compile(r"<a\b[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL)
_HTML_ANCHOR_TAG_RE = re.compile(r"</?a\b[^>]*>", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s)>\]\"']+", re.IGNORECASE)
_COMMIT_SHA_RE = re.compile(r"\b[0-9a-fA-F]{7,40}\b")
_WHITESPACE_RE = re.compile(r"\s+")


def _bot_slug(login: str | None) -> str:
    """Return the canonical login with any trailing `[bot]` suffix stripped."""
    if not login:
        return ""
    return login[:-5] if login.endswith("[bot]") else login


def _is_bot(login: str | None, bot_logins: frozenset[str]) -> bool:
    """True when `login` matches an entry in `bot_logins` (with or without `[bot]`)."""
    if not login:
        return False
    slug = _bot_slug(login)
    return login in bot_logins or slug in bot_logins or f"{slug}[bot]" in bot_logins


def normalise_finding_text(body: str) -> str:
    """Normalise a review comment body so re-posts of the same finding match.

    Bots such as `qltysh` re-post identical finding prose on every push and
    append a trailing `<a href="https://qlty.sh/...">` link (or bare URL /
    commit SHA) that embeds the posting commit or run ID. Stripping HTML
    comments, `<a ...>...</a>` elements, bare URLs, and 7..40-hex commit
    SHAs before collapsing whitespace turns per-push copies of the same
    finding into one canonical string.
    """
    text = _HTML_COMMENT_RE.sub(" ", body)
    text = _HTML_LINK_RE.sub(" ", text)
    text = _HTML_ANCHOR_TAG_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _COMMIT_SHA_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def finding_key(
    thread: dict[str, Any],
) -> tuple[str, str, str, int | None] | None:
    """Return the normalised cross-push dedupe key for a thread.

    The key is `(author, path, normalised_finding_text, line_if_not_outdated)`:
      * `author`: canonical bot login (`[bot]` suffix stripped).
      * `path`: repo-relative file path.
      * `text`: `normalise_finding_text(thread["body"])`.
      * `line`: `int(thread["line"])` when the thread is NOT outdated, or
        `None` when `thread["isOutdated"]` is true (because an outdated
        thread's line anchor shifted in a later push).

    Returns `None` for threads that lack an author, path, finding text, or
    (when not outdated) a line anchor (file-level threads).
    """
    author = _bot_slug(thread.get("author"))
    path = thread.get("path")
    if not author or not path:
        return None
    is_outdated = bool(thread.get("isOutdated"))
    raw_line = thread.get("line")
    if not is_outdated and raw_line is None:
        return None
    text = normalise_finding_text(thread.get("body") or "")
    if not text:
        return None
    line: int | None = None if is_outdated else int(raw_line)
    return (author, path, text, line)


def _gh_graphql_field_args(key: str, value: Any) -> list[str]:
    """Serialize one (key, value) GraphQL variable as `gh api graphql` flags.

    Three forms of `gh api graphql` are in play here, picked by the value's
    Python type so the server sees the type the variable declares:

    * `str`              -> `-f key=value`            (shipped as String)
    * `bool` / `int`     -> `-F key=value`            (shipped as JSON-typed)
    * `list`             -> `-F key[]=item` per item  (shipped as a JSON array)
    * anything else      -> rejected -- the script's own GraphQL variables
                            are exactly these four types, and a wider
                            contract here would mean a wider blast radius
                            in `_gh_graphql`.

    The old shape used `-f` for every scalar, which made every variable a
    String on the wire. `--pr` is parsed as `type=int` and the query
    declares `$pr:Int!`, so `-f pr=761` rejected the whole document with
    `Variable $pr of type Int! was provided invalid value` and the script
    crashed on every run before doing any work. `gh api graphql -F`
    converts numbers, booleans and lists to their JSON-typed equivalents
    on the wire (verified against the live API on 2026-09-19), which is
    what `Int!` and `[ID!]!` actually want.

    Note: `-F key=value` for a list value ALSO accepts JSON (`-F ids='["a","b"]'`)
    and parses it as a JSON array, but we use the `key[]=v` repeat form
    because it composes cleanly with the existing single-value path --
    no JSON-escaping decisions to make per call site.
    """
    if isinstance(value, bool):
        # `bool` is a subclass of `int`; check it first so `True`/`False`
        # don't fall through to the int branch.
        return ["-F", f"{key}={'true' if value else 'false'}"]
    if isinstance(value, str):
        return ["-f", f"{key}={value}"]
    if isinstance(value, int):
        return ["-F", f"{key}={value}"]
    if isinstance(value, list):
        if not value:
            # `gh api graphql -F key[]` (no value) is the documented way to
            # ship an empty list -- the script never passes one today,
            # but the path is one extra branch and keeps the helper
            # honest about what `list[str]` means.
            return ["-F", f"{key}[]"]
        out: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise TypeError(
                    f"_gh_graphql list variable {key!r} must contain "
                    f"strings; got {type(item).__name__}"
                )
            out.extend(["-F", f"{key}[]={item}"])
        return out
    raise TypeError(
        f"_gh_graphql does not know how to ship a {type(value).__name__} "
        f"variable for key {key!r}; add a branch for it before passing "
        f"one in."
    )


def _gh_graphql(query: str, **fields: Any) -> dict[str, Any]:
    """Run one GraphQL query via `gh api graphql`.

    Field values are coerced through `_gh_graphql_field_args`, which
    selects `-f` (String), `-F` (Int / Bool / list) or `--input` (JSON
    body) per the value's Python type. Pagination is the caller's
    responsibility; this helper runs one page.

    Raises RuntimeError on a non-200 response or on a response carrying a
    non-empty `.errors` list. The gate's collect step fails closed on the
    same shape; this script follows suit so a malformed query never
    silently processes nothing.
    """
    args: list[str] = ["gh", "api", "graphql"]
    for key, value in fields.items():
        args.extend(_gh_graphql_field_args(key, value))
    args.extend(["-f", f"query={query}"])
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"gh api graphql failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    try:
        payload = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"gh api graphql returned non-JSON: {exc}") from exc
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"gh api graphql returned errors: {errors}")
    return payload


def fetch_threads(owner: str, repo: str, pr: int) -> list[dict[str, Any]]:
    """Fetch every review thread for a PR, paginated.

    Each thread carries: id, isResolved, isOutdated, the path and line of
    the thread's first comment, the author login of that comment, the
    comment's url and body, and that comment's createdAt so we can break
    ties by arrival order.

    `createdAt` comes from the COMMENT because the thread does not have one
    -- see the note at the extraction below.
    """
    query = """
    query($owner:String!,$repo:String!,$pr:Int!,$endCursor:String){
      repository(owner:$owner,name:$repo){
        pullRequest(number:$pr){
          reviewThreads(first:100,after:$endCursor){
            pageInfo{hasNextPage endCursor}
            nodes{
              id
              isResolved
              isOutdated
              comments(first:1){
                nodes{
                  author{login}
                  path
                  line
                  originalLine
                  body
                  url
                  databaseId
                  createdAt
                }
              }
            }
          }
        }
      }
    }
    """
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        fields: dict[str, Any] = {"owner": owner, "repo": repo, "pr": pr}
        if cursor is not None:
            fields["endCursor"] = cursor
        payload = _gh_graphql(query, **fields)
        thread_page = (
            payload["data"]["repository"]["pullRequest"]["reviewThreads"]
        )
        for node in thread_page["nodes"]:
            comment = (node.get("comments", {}).get("nodes") or [{}])[0]
            out.append(
                {
                    "id": node["id"],
                    "isResolved": bool(node.get("isResolved")),
                    "isOutdated": bool(node.get("isOutdated")),
                    # From the COMMENT, not the thread.
                    # `PullRequestReviewThread` has no `createdAt` field --
                    # introspection lists only comments, diffSide, id,
                    # isCollapsed, isOutdated, isResolved, line,
                    # originalLine, originalStartLine, path, pullRequest,
                    # repository, resolvedBy, startDiffSide, startLine,
                    # subjectType and the viewerCan* flags. Selecting it on
                    # the node made the server reject the WHOLE document
                    # with `Field 'createdAt' doesn't exist on type
                    # 'PullRequestReviewThread'`, so this script has never
                    # once completed a run. The first comment's timestamp is
                    # the thread's arrival order anyway, which is what the
                    # tie-break wants.
                    "createdAt": comment.get("createdAt") or "",
                    "author": comment.get("author", {}).get("login"),
                    "path": comment.get("path"),
                    # `line` is null on file-level threads and on outdated
                    # threads; `originalLine` is what the bot originally
                    # anchored to and is the value that survives an edit.
                    "line": comment.get("line") or comment.get("originalLine"),
                    "body": comment.get("body") or "",
                    "url": comment.get("url"),
                    "comment_database_id": comment.get("databaseId"),
                }
            )
        page_info = thread_page["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return out


def fetch_thread_bodies(
    owner: str, repo: str, pr: int, thread_ids: list[str]
) -> dict[str, str]:
    """Fetch the full comment bodies of the threads we plan to reply to.

    A thread's `comments(first:1)` only returns the FIRST comment, but the
    reply we post lands as a later comment on the same thread. To detect
    "we already replied" we need the FULL comment list, not just the
    opener. This is a separate, narrower query: the threads we care about
    are the candidates for resolution, not all threads on the PR.

    Returns {thread_id: concatenated_bodies}. The bodies are joined with
    newlines so the sentinel grep is unambiguous.

    ONE GraphQL round-trip for the whole list, via the top-level
    `nodes(ids: [ID!]!)` field -- `PullRequest.reviewThreads(ids:)` is
    NOT a real argument (verified against the live schema on 2026-09-19:
    `__type(name:"PullRequest").fields` lists only `after, before, first,
    last` for `reviewThreads`), but `Query.nodes(ids:)` is, and filtering
    through `... on PullRequestReviewThread` is how the docstring's
    original "single round-trip for N threads" intent lands. Smaller
    than an N-query loop, and the cost is one round-trip per run rather
    than N -- the old shape paid one round-trip per duplicate, which was
    the bottleneck Codacy flagged on PR #723.

    `owner` / `repo` / `pr` are kept on the signature for caller
    uniformity with `fetch_threads` even though `Query.nodes` doesn't
    need them; the GraphQL variables declared in the document track the
    old shape so the byte-identity copy in maxi-config stays a
    one-character substitution. Pinned by
    `test_fetch_thread_bodies_signature_includes_pr_coords` so a future
    refactor that drops them only lands if it also updates the docstring
    contract above.
    """
    if not thread_ids:
        return {}
    bodies: dict[str, str] = {}
    # `Query.nodes(ids: [ID!]!)` is a real top-level field (verified
    # against the live schema on 2026-09-19 -- `PullRequest.reviewThreads`
    # only takes `after, before, first, last`, never `ids:`). The body of
    # this query does not need PR coordinates, so the document does not
    # declare them. `fetch_thread_bodies`'s signature still carries
    # `(owner, repo, pr)` for caller uniformity with `fetch_threads`; the
    # docstring contract above spells out why we keep them.
    query = """
    query($ids:[ID!]!){
      nodes(ids:$ids){
        ... on PullRequestReviewThread{
          id
          comments(first:50){
            nodes{ body }
          }
        }
      }
    }
    """
    for start in range(0, len(thread_ids), _GRAPHQL_NODES_BATCH_SIZE):
        batch = thread_ids[start : start + _GRAPHQL_NODES_BATCH_SIZE]
        payload = _gh_graphql(query, ids=batch)
        thread_nodes = (payload.get("data") or {}).get("nodes") or []
        for thread in thread_nodes:
            if not thread:
                # `nodes(ids:)` echoes a `null` slot for every ID that does
                # not resolve to the requested type or is unknown; the inner
                # block is skipped rather than crashed on so a stale ID
                # doesn't kill the whole run.
                continue
            thread_id = thread.get("id")
            if not thread_id:
                continue
            bodies[thread_id] = "\n".join(
                (node.get("body") or "")
                for node in (thread.get("comments") or {}).get("nodes") or []
            )
    return bodies


def cluster(
    threads: list[dict[str, Any]],
    bot_logins: frozenset[str],
    *,
    window: int = WINDOW,
) -> list[list[dict[str, Any]]]:
    """Group bot threads into duplicate clusters `[keeper, *duplicates]`.

    Runs in two passes:

    1. Cross-push / same-key deduplication (single-bot re-posts across or
       within rounds):
       For bot threads whose `finding_key(thread)` is defined, group by
       `(author, path, normalised_finding_text, line_if_not_outdated)`
       (when either thread in a `(author, path, text)` bucket is outdated,
       its `line` is `None` and matches on `(author, path, text)`). A
       bucket that holds an outdated copy keeps matching EVERY line of
       that `(author, path, text)` key, because the outdated anchor
       shifted and the re-post that followed it landed on a new line.
       Order each group oldest-first (`createdAt`, then already-resolved
       before unresolved on timestamp ties, then input index). Pick as
       `keeper` the earliest thread in the group that was not resolved
       solely by our own dedupe sentinel. Every unresolved thread after
       `keeper` is resolved as a duplicate of `keeper`:
         * If the oldest copy is open, it stays open and all newer copies
           resolve against it (never resolving the last remaining copy).
         * If the oldest copy was already resolved by a human/agent
           decision, newer unresolved copies are re-posts of a decided
           finding and resolve against it too.

    2. Same-round multi-bot line-window deduplication:
       Among the remaining unresolved bot threads that have a `(path, line)`
       anchor -- the same-key DUPLICATES are excluded (they already
       resolve against their keeper), but their KEEPER is not -- two
       threads cluster when they share a `path` AND their lines differ by
       at most `2*window`. Connected components with `>= 2` distinct bot
       authors form clusters ordered by `createdAt`; a component that
       holds a same-key keeper is headed by that keeper, so the cross-bot
       thread sharing its line window resolves against it;
       single-author components with distinct finding keys are dropped so
       distinct findings by the same bot in one line window are never
       collapsed against each other.

    Human-authored threads, threads from logins outside `bot_logins`, and
    file-level threads (`line is None` and not outdated) are never touched.
    """
    clusters: list[list[dict[str, Any]]] = []

    # Pass 1: cross-push / within-round same-key bot deduplication.
    keyed_candidates: list[
        tuple[tuple[str, int, int], dict[str, Any], tuple[str, str, str, int | None]]
    ] = []
    for idx, thread in enumerate(threads):
        if not _is_bot(thread.get("author"), bot_logins):
            continue
        key = finding_key(thread)
        if key is None:
            continue
        sort_key = (
            thread.get("createdAt") or "",
            0 if thread.get("isResolved") else 1,
            idx,
        )
        keyed_candidates.append((sort_key, thread, key))

    keyed_candidates.sort(key=lambda item: item[0])

    by_base_key: dict[
        tuple[str, str, str], list[tuple[int | None, list[dict[str, Any]]]]
    ] = defaultdict(list)
    for _sort_key, thread, key in keyed_candidates:
        author, path, text, line = key
        base = (author, path, text)
        buckets = by_base_key[base]
        matched_idx: int | None = None
        for idx_b, (b_line, _group) in enumerate(buckets):
            if b_line == line or b_line is None or line is None:
                matched_idx = idx_b
                break
        if matched_idx is None:
            buckets.append((line, [thread]))
            continue
        b_line, group = buckets[matched_idx]
        group.append(thread)
        # A bucket that holds an OUTDATED copy stands for every line of
        # the finding: an outdated thread's anchor shifted, so it matches
        # the copies before and after the shift whatever line they sit
        # on. Pinning it to the line of whichever copy joined it split
        # shifted re-posts into separate groups, and those groups stayed
        # open because a group of one is never a cluster.
        if line is None or b_line is None:
            buckets[matched_idx] = (None, group)

    # Same-key duplicates. They already resolve against their keeper in
    # pass 1, so pass 2 must not see them again.
    key_dup_ids: set[str] = set()
    # Same-key keepers -- the surviving copy of a finding key. They stay
    # eligible for the line-window pass: excluding them left a cross-bot
    # thread that shared the site with no partner to resolve against, and
    # that thread stayed open forever.
    key_keeper_ids: set[str] = set()
    for buckets in by_base_key.values():
        for _b_line, group in buckets:
            if len(group) < 2:
                continue
            keeper_idx: int | None = None
            for idx_g, candidate in enumerate(group):
                if candidate.get("isResolved") and _already_processed(
                    candidate.get("body") or ""
                ):
                    continue
                keeper_idx = idx_g
                break
            if keeper_idx is None:
                continue
            keeper = group[keeper_idx]
            dups = [
                t for t in group[keeper_idx + 1 :] if not t.get("isResolved")
            ]
            if not dups:
                continue
            clusters.append([keeper, *dups])
            key_keeper_ids.add(keeper["id"])
            for dup in dups:
                key_dup_ids.add(dup["id"])

    # Pass 2: same-round multi-bot line-window deduplication.
    max_gap = 2 * window
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for thread in threads:
        if thread["id"] in key_dup_ids:
            continue
        if thread["isResolved"]:
            continue
        if not _is_bot(thread.get("author"), bot_logins):
            continue
        path = thread.get("path")
        line = thread.get("line")
        if not path or line is None:
            continue
        by_path[path].append(thread)

    for members in by_path.values():
        # Sort by line so adjacent members whose lines differ by <=
        # max_gap are the only ones that can ever pair. A single sweep
        # over the sorted list catches every adjacency in O(N) and
        # union-find fills in the transitive edges.
        ordered = sorted(members, key=lambda t: int(t["line"]))
        # Union-find over the ordered list. We union neighbours whose
        # lines are within max_gap, then collect the connected
        # components. Components with >= 2 distinct authors are
        # clusters; the others are bot loops and are dropped.
        parent = list(range(len(ordered)))

        def find(idx: int) -> int:
            while parent[idx] != idx:
                parent[idx] = parent[parent[idx]]
                idx = parent[idx]
            return idx

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(len(ordered) - 1):
            if int(ordered[i + 1]["line"]) - int(ordered[i]["line"]) <= max_gap:
                union(i, i + 1)

        groups: dict[int, list[int]] = defaultdict(list)
        for idx in range(len(ordered)):
            groups[find(idx)].append(idx)

        for indices in groups.values():
            component = [ordered[k] for k in indices]
            authors = {_bot_slug(t["author"]) for t in component}
            if len(authors) < 2:
                continue
            component.sort(key=lambda t: t["createdAt"])
            # A same-key keeper is the surviving copy of its finding key,
            # so it ANCHORS the cross-bot cluster: it heads the cluster
            # and stays open, and the cross-bot thread that shares its
            # line window resolves against it. Resolving the keeper
            # instead would close the last open copy of that key, and
            # leaving the keeper out of the window pass altogether left
            # the cross-bot thread with nobody to resolve against.
            anchor = next(
                (t for t in component if t["id"] in key_keeper_ids), None
            )
            if anchor is not None:
                # A second same-key keeper keeps its own open copy: it is
                # the survivor of its own key, not a duplicate of the
                # anchor.
                rest = [
                    t
                    for t in component
                    if t["id"] != anchor["id"] and t["id"] not in key_keeper_ids
                ]
                if not rest:
                    continue
                component = [anchor, *rest]
            clusters.append(component)
    return clusters


def _already_processed(body: str) -> bool:
    """True if `body` carries our sentinel.

    `body` is the concatenated bodies of every comment on a thread. The
    sentinel is hidden, so a human reader does not see it; a re-run does.
    """
    return SENTINEL in body


def plan(
    threads: list[dict[str, Any]],
    bot_logins: frozenset[str],
    *,
    fetch_bodies: "Callable[[list[str]], dict[str, str]] | None" = None,
    window: int = WINDOW,
) -> tuple[
    list[dict[str, Any]],
    list[tuple[dict[str, Any], dict[str, Any]]],
    list[dict[str, Any]],
]:
    """Decide what to keep, what to resolve, and what was already done.

    Returns (kept, to_resolve, already_processed) where:
      * kept is the list of keeper threads -- one per surviving thread,
        not one per cluster. A same-key keeper that also anchors a
        cross-bot cluster heads both, and it is listed once because it
        stays open either way.
      * to_resolve is a list of (duplicate, keeper) pairs -- the
        association is part of the value, not a property to be
        re-derived at apply time. A flat (dup, ...) list would force
        the apply path to look up the keeper, which is wrong when one
        path has more than one cluster (each duplicate belongs to a
        SPECIFIC cluster's keeper, not to whichever keeper of that path
        happens to sort first).
      * already_processed is the list of duplicates whose sentinel reply
        a previous run posted AND whose thread is already resolved --
        work a previous run completed in full. A duplicate with the
        sentinel but an UNRESOLVED thread is a previous run whose
        resolve mutation failed; it goes to to_resolve instead, so the
        re-run retries the resolve without re-posting the reply.

    `fetch_bodies` is an injected dependency: given a list of thread IDs,
    return {thread_id: concatenated_body}. In production it is a closure
    over (owner, repo, pr); in tests it is a fixture lookup. When
    `fetch_bodies` is None, every duplicate is treated as "not yet
    processed" -- which is the right answer for the dry-run path that
    does not need to distinguish, and saves a GraphQL roundtrip when the
    script is invoked with --payload (offline).
    """
    kept: list[dict[str, Any]] = []
    kept_ids: set[str] = set()
    to_resolve: list[tuple[dict[str, Any], dict[str, Any]]] = []
    already_processed: list[dict[str, Any]] = []

    clusters = cluster(threads, bot_logins, window=window)
    candidate_ids = [
        thread["id"]
        for cluster_ in clusters
        for thread in cluster_[1:]  # all but the kept (earliest) thread
    ]
    bodies = fetch_bodies(candidate_ids) if fetch_bodies else {}

    for cluster_ in clusters:
        keeper = cluster_[0]
        if keeper["id"] not in kept_ids:
            kept_ids.add(keeper["id"])
            kept.append(keeper)
        for dup in cluster_[1:]:
            if not _already_processed(bodies.get(dup["id"], "")):
                to_resolve.append((dup, keeper))
                continue
            # The sentinel is in the body, so a previous run already
            # posted the reply. The resolve may still have failed --
            # reply and resolve are two separate mutations, and the
            # one that succeeded is not proof of the other. A
            # duplicate that reached this loop passed cluster()'s
            # isResolved filter (the cluster input is unresolved
            # threads only), so this is the recoverable half of the
            # split-brain: retry the resolve WITHOUT re-posting the
            # reply. The already_processed branch below is the
            # settled case -- sentinel present AND thread resolved --
            # and is kept (rather than left to the filter, which
            # would have dropped the thread before plan() saw it) so
            # the report still names what a previous run completed.
            # cluster() drops isResolved threads defensively: a race
            # (thread resolved between the two queries) or a hand-
            # built fixture without the field can still put a
            # resolved duplicate in front of this loop, and routing
            # it into to_resolve would resolve an already-resolved
            # thread -- harmless to GitHub but a wasted mutation and
            # a misleading `resolved` count on the report.
            if dup.get("isResolved"):
                already_processed.append(dup)
            else:
                to_resolve.append((dup, keeper))
    return kept, to_resolve, already_processed


def _reply_body(keeper: dict[str, Any], dup: dict[str, Any]) -> str:
    """Build the duplicate-of reply body for a (keeper, dup) pair.

    Public for tests; kept separate from reply_and_resolve so the body
    shape can be asserted without exercising the GraphQL mutations. The
    leading sentinel is hidden to humans (HTML comment) and visible to
    a re-run; the rest of the sentence names the keeper's URL and
    author so a reader understands what they are looking at.
    """
    keeper_url = keeper.get("url") or ""
    keeper_author = keeper.get("author") or "earlier thread"
    return (
        f"{SENTINEL}\n"
        f"Duplicate of {keeper_url} ({keeper_author}) -- same file:line "
        f"window; tracked there."
    )


def reply_and_resolve(
    owner: str,
    repo: str,
    pr: int,
    thread: dict[str, Any],
    keeper: dict[str, Any],
    *,
    already_replied: bool = False,
) -> None:
    """Post the duplicate-of reply and resolve the thread.

    Idempotent at the per-thread level. With `already_replied=False`
    (first visit) both mutations run: reply, then resolve. With
    `already_replied=True` -- set by the apply path when plan() found
    our sentinel in the thread body but the thread is still unresolved
    -- ONLY the resolve runs. That is the recovered half of the
    two-mutation split-brain: a previous run posted the reply and its
    resolve mutation failed, and re-posting the reply would leave a
    second duplicate-of comment under the first. The resolve itself is
    idempotent (an already-resolved thread returns success). Two-step
    rather than atomic because GitHub's GraphQL has no single mutation
    for "reply + resolve", and the failure mode of "reply posted,
    resolve failed" is recoverable on the next run -- which is the
    path `already_replied=True` exists to take.
    """
    reply_body = _reply_body(keeper, thread)

    # The asymmetry below is GitHub's, not ours:
    # AddPullRequestReviewThreadReplyInput's field is
    # `pullRequestReviewThreadId`, while ResolveReviewThreadInput's is
    # plain `threadId`. Spelling the reply one `threadId` is accepted by
    # no schema and fails the whole mutation with a three-error cascade
    # whose LAST line -- the one the action wrapper surfaces -- is the
    # least informative of the three:
    #
    #   Argument 'pullRequestReviewThreadId' on InputObject ... is required
    #   InputObject ... doesn't accept argument 'threadId'
    #   Variable $threadId is declared by anonymous mutation but not used
    #
    # The middle line is the cause; the third is what got reported.
    reply_query = '''
    mutation($threadId:ID!,$body:String!){
      addPullRequestReviewThreadReply(input:{pullRequestReviewThreadId:$threadId,body:$body}){
        comment{ id }
      }
    }
    '''
    if not already_replied:
        _gh_graphql(reply_query, threadId=thread["id"], body=reply_body)

    resolve_query = """
    mutation($threadId:ID!){
      resolveReviewThread(input:{threadId:$threadId}){
        thread{ id isResolved }
      }
    }
    """
    _gh_graphql(resolve_query, threadId=thread["id"])


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Deduplicate bot review threads that hit the same lines in "
            "the same round. Default is dry-run; pass --apply to write."
        ),
    )
    parser.add_argument("--owner", default=os.environ.get("GITHUB_REPOSITORY_OWNER", ""))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1] if os.environ.get("GITHUB_REPOSITORY") else "")
    parser.add_argument("--pr", type=int, default=_pr_from_env())
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN", ""))
    parser.add_argument("--window", type=int, default=WINDOW)
    parser.add_argument("--bot", action="append", default=[], help="Extra bot login to dedupe (repeatable).")
    parser.add_argument("--apply", action="store_true", help="Post replies and resolve threads.")
    parser.add_argument(
        "--payload",
        default=None,
        help=(
            "Skip the GraphQL fetch and read threads from this file (a "
            "JSON array of thread dicts in the shape fetch_threads "
            "returns). Useful for the test harness and for offline "
            "development."
        ),
    )
    return parser.parse_args(argv)


def _pr_from_env() -> int:
    candidates = (
        os.environ.get("PR_NUMBER"),
        os.environ.get("GITHUB_EVENT_PULL_REQUEST_NUMBER"),
        os.environ.get("GITHUB_PR_NUMBER"),
    )
    for value in candidates:
        if value:
            try:
                return int(value)
            except ValueError:
                continue
    return 0


def main(argv: list[str]) -> int:
    # argparse treats argv[0] as the program name when you call it with
    # no arguments; pass argv[1:] in so a `python3 dedupe-review-threads.py
    # --apply` invocation works AND the test harness can pass either form.
    if argv and argv[0].endswith(".py"):
        argv = argv[1:]
    args = parse_args(argv)

    # `--token` was parsed and then ignored: every `gh` call below inherits
    # whatever GH_TOKEN the environment already carries, so passing a token
    # explicitly did nothing and failed as an authentication error nobody
    # could explain from the flag they had set. Export it before the first
    # GraphQL call. (codacy, on maxi-config#797, which vendors this file.)
    #
    # The default comes from GITHUB_TOKEN or GH_TOKEN, so the common case
    # writes the same value back and the assignment is a no-op.
    if args.token:
        os.environ["GH_TOKEN"] = args.token

    if not args.owner or not args.repo or not args.pr:
        print(
            "::error::owner/repo/pr are required (set --owner/--repo/--pr or "
            "GITHUB_REPOSITORY + PR_NUMBER)",
            file=sys.stderr,
        )
        return 2

    bot_logins = frozenset(BOT_LOGINS) | frozenset(args.bot)

    if args.payload:
        with open(args.payload, "r", encoding="utf-8") as fh:
            threads = json.load(fh)
    else:
        threads = fetch_threads(args.owner, args.repo, args.pr)

    # Memoised across plan() and the apply loop below. plan() fetches
    # the candidate bodies once (one batched round-trip), and the apply
    # loop needs the SAME answer -- which duplicates already carry the
    # sentinel -- to retry resolve-only without re-posting the reply.
    # A second fetch would be a second round-trip for data this run
    # already holds, and a re-fetch mid-run could even race the reply
    # this run is about to post. Keyed by thread ID; entries are the
    # concatenated comment bodies as of the start of this run.
    bodies_cache: dict[str, str] = {}

    def _memoised_fetch(ids: list[str]) -> dict[str, str]:
        missing = [i for i in ids if i not in bodies_cache]
        if missing:
            bodies_cache.update(
                fetch_thread_bodies(args.owner, args.repo, args.pr, missing)
            )
        return {i: bodies_cache[i] for i in ids if i in bodies_cache}

    fetch_bodies: Callable[[list[str]], dict[str, str]] | None
    if args.payload is not None:
        # Fixture data: bodies are not in the fixture file (we keep the
        # fixture small) so we cannot detect "already processed" without
        # an extra channel. The whole point of the fixture is offline
        # testing of the cluster/plan shape, not the bodies fetch.
        fetch_bodies = None
    else:
        # Both live paths (apply and dry-run) use the memoised fetch.
        # The apply path needs bodies to skip threads we already
        # replied to -- otherwise it re-posts replies on every re-run
        # -- and, since the resolve-retry fix, to retry resolve-only on
        # the ones whose reply landed but whose resolve didn't. The
        # dry-run path reports "needs resolution" vs "already
        # processed", which costs the same single call and makes the
        # report actually useful for the reviewer who sees it on the
        # PR page.
        fetch_bodies = _memoised_fetch

    kept, to_resolve, already_processed = plan(
        threads, bot_logins, fetch_bodies=fetch_bodies, window=args.window
    )

    if len(to_resolve) > MAX_RESOLVE_PER_RUN:
        print(
            f"::error::refusing to resolve {len(to_resolve)} threads in one "
            f"run (cap {MAX_RESOLVE_PER_RUN}); investigate the PR manually",
            file=sys.stderr,
        )
        return 3

    if args.apply:
        # plan() returns (dup, keeper) tuples so the apply path does
        # NOT need to re-derive the keeper from the path -- a path with
        # more than one cluster would map every duplicate to whichever
        # keeper of that path sorted first, which is the wrong keeper
        # for any later cluster.
        for dup, keeper in to_resolve:
            reply_and_resolve(
                args.owner,
                args.repo,
                args.pr,
                dup,
                keeper,
                # Resolve-only retry when a previous run already posted
                # the reply (sentinel in the body) and only the resolve
                # failed. `bodies_cache` holds what plan() fetched; it
                # is None/empty on the --payload offline path, whose
                # fixtures are unmarked by construction.
                already_replied=_already_processed(
                    bodies_cache.get(dup["id"], "")
                ),
            )

    report = {
        "owner": args.owner,
        "repo": args.repo,
        "pr": args.pr,
        "window": args.window,
        "bot_logins": sorted(bot_logins),
        "applied": args.apply,
        "kept": [_summary(t) for t in kept],
        "resolved": [_summary(dup) for dup, _keeper in to_resolve],
        "already_processed": [_summary(t) for t in already_processed],
    }
    print(json.dumps(report, indent=2))
    return 0


def _summary(thread: dict[str, Any]) -> dict[str, Any]:
    """Reduce a thread to the fields the report needs."""
    return {
        "id": thread["id"],
        "author": thread.get("author"),
        "path": thread.get("path"),
        "line": thread.get("line"),
        "url": thread.get("url"),
    }


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
