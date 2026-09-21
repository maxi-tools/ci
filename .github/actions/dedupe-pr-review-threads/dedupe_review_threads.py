#!/usr/bin/env python3
"""Deduplicate bot review threads that hit the same lines in the same round.

When two or more review bots flag the same code in the same round, every
thread still has to be replied to and resolved before review-gate goes
green. Resolving them by hand is mechanical work the bots do not help with,
and the round where a human reviewer should be designing instead reads as a
thread-resolution sweep.

Measured on maxi-core 2026-09-16..18: 55 bot comments across 9 PRs, 8 sites
with two or more bots in the same 8-line window, one with three (Codacy +
qlty + maxi-reviewer on scripts/fleet/deploy-member.sh). Every duplicate is
a thread the PR author has to handle before merge, and the handler is always
the same shape: identify the earlier thread, mark the later ones as
duplicates, and resolve them.

This script does that handler.

WHAT IT TOUCHES
  * Resolves review threads that THIS script previously resolved, on re-run.
  * Resolves bot review threads whose author is one of the five bot logins
    in BOT_LOGINS, when they share a (path, +-4 line window) with another
    bot thread from a distinct bot login in the same window.
  * Posts one reply on each thread it resolves, citing the kept thread.

WHAT IT DOES NOT TOUCH
  * Human-authored threads. Filtered before clustering.
  * Already-resolved threads. Filtered before clustering.
  * File-level threads (no line number). Kept -- a file-level flag is a
    different conversation even when two bots raise it, because neither
    points at code.
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
one must do nothing. State alone does not pin this: a thread can be left
"resolved by us" and a third party can re-open it, or the reply can succeed
while the resolve fails (a network blip in between). The reply carries a
hidden HTML comment `<!-- maxi-config:dedupe -->`, so a re-run that finds a
thread with our sentinel in its comment body skips the work -- whether the
thread is currently resolved or not -- and re-runs only on threads we have
not touched.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from collections.abc import Callable
from typing import Any


#: Bots active in the org whose threads we deduplicate.
#:
#: Five, not three. The original three (codacy-production, coderabbitai,
#: maxi-reviewer) cover the well-known lanes; cubic-dev-ai (the
#: `cubic . AI code reviewer` GitHub App) and chatgpt-codex-connector (the
#: ChatGPT Codex Connector GitHub App) post prolifically on this org's
#: PRs and were left out by the original dedupe design, which is the gap
#: this script fills. Verified on a live maxi-core PR with five-bot
#: co-location: codacy-production, coderabbitai, maxi-reviewer,
#: cubic-dev-ai, chatgpt-codex-connector all on the same window.
BOT_LOGINS: tuple[str, ...] = (
    "codacy-production",
    "coderabbitai",
    "maxi-reviewer",
    "cubic-dev-ai",
    "chatgpt-codex-connector",
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

#: Cap on threads we will process in one run. A single PR with hundreds of
#: unresolved bot threads is almost certainly a bot loop, not a real review;
#: bailing out is safer than touching every thread on it.
MAX_RESOLVE_PER_RUN = 100


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
    payload = _gh_graphql(query, ids=thread_ids)
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
    """Group unresolved bot threads into clusters.

    Two threads cluster when they share a path AND their lines differ
    by at most 2*window. window=4 -> +-4 -> lines 10 and 18 share a
    cluster (|18-10| = 8 = 2*window); lines 10 and 19 do not.

    A cluster is a maximal connected component under that relation:
    if A is in the same window as B, and B is in the same window as C,
    then A, B and C share a cluster even if A and C are NOT in the
    same window -- because the duplicate-of reply goes on the LATER
    thread and points at the EARLIEST, and an extra middle thread is
    not a second conversation.

    A cluster contains at least two distinct bot authors. Single-author
    clusters are NOT returned: a bot posting twice on the same lines is
    a bot loop, not a duplicate, and deduping its output against itself
    would suppress findings the reviewer should see.

    Threads that are already resolved, or that are not from a bot login,
    or that lack a (path, line) anchor, are NOT returned.

    Clustering is O(N log N) on the per-path sort then O(N) within each
    path bucket (a single sweep over the line-sorted list with
    union-find on adjacent pairs whose lines differ by < max_gap).
    Equivalent to the naive O(N^2) union-the-pairs-within-gap, because
    union-find is transitive: if A is within gap of B and B is within
    gap of C, then A and C share a cluster whether or not they are
    within gap of each other directly. Path buckets are small on real
    PRs -- the measurement that motivated this script found at most
    three threads per window, and a ten-thread bucket is the worst
    case observed in two months of data.
    """
    max_gap = 2 * window
    by_path: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for thread in threads:
        if thread["isResolved"]:
            continue
        if thread.get("author") not in bot_logins:
            continue
        path = thread.get("path")
        line = thread.get("line")
        if not path or line is None:
            continue
        by_path[path].append(thread)

    clusters: list[list[dict[str, Any]]] = []
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
            authors = {t["author"] for t in component}
            if len(authors) < 2:
                continue
            component.sort(key=lambda t: t["createdAt"])
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
      * kept is the list of keeper threads (one per cluster),
      * to_resolve is a list of (duplicate, keeper) pairs -- the
        association is part of the value, not a property to be
        re-derived at apply time. A flat (dup, ...) list would force
        the apply path to look up the keeper, which is wrong when one
        path has more than one cluster (each duplicate belongs to a
        SPECIFIC cluster's keeper, not to whichever keeper of that path
        happens to sort first).
      * already_processed is the list of duplicates our sentinel has
        already replied to on a previous run.

    `fetch_bodies` is an injected dependency: given a list of thread IDs,
    return {thread_id: concatenated_body}. In production it is a closure
    over (owner, repo, pr); in tests it is a fixture lookup. When
    `fetch_bodies` is None, every duplicate is treated as "not yet
    processed" -- which is the right answer for the dry-run path that
    does not need to distinguish, and saves a GraphQL roundtrip when the
    script is invoked with --payload (offline).
    """
    kept: list[dict[str, Any]] = []
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
        kept.append(keeper)
        for dup in cluster_[1:]:
            if _already_processed(bodies.get(dup["id"], "")):
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
) -> None:
    """Post the duplicate-of reply and resolve the thread.

    Idempotent at the per-thread level: if the reply is already present
    (sentinel in the body), we still try the resolve, which is itself
    idempotent (an already-resolved thread returns success). Two-step
    rather than atomic because GitHub's GraphQL has no single mutation
    for "reply + resolve", and the failure mode of "reply posted, resolve
    failed" is recoverable on the next run.
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

    fetch_bodies: Callable[[list[str]], dict[str, str]] | None
    if args.payload is not None:
        # Fixture data: bodies are not in the fixture file (we keep the
        # fixture small) so we cannot detect "already processed" without
        # an extra channel. The whole point of the fixture is offline
        # testing of the cluster/plan shape, not the bodies fetch.
        fetch_bodies = None
    elif args.apply:
        # Live apply path: bodies are necessary to skip threads we
        # already replied to, otherwise the apply path re-posts replies
        # on every re-run.
        fetch_bodies = lambda ids: fetch_thread_bodies(  # noqa: E731
            args.owner, args.repo, args.pr, ids
        )
    else:
        # Live dry-run path: report distinguishes "needs resolution"
        # from "already processed" when the bodies fetch is available.
        # It costs one GraphQL call per run, which is cheap on this
        # lane and makes the report actually useful for the reviewer
        # who sees it on the PR page.
        fetch_bodies = lambda ids: fetch_thread_bodies(  # noqa: E731
            args.owner, args.repo, args.pr, ids
        )

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
            reply_and_resolve(args.owner, args.repo, args.pr, dup, keeper)

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
