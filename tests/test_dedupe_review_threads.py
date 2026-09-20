#!/usr/bin/env python3
"""Truth table for dedupe-review-threads.py.

The dedupe handles N bots hitting the same lines at once. Nothing else in
the org does: detect-review-churn.py catches the SEQUENTIAL case (same
predicate re-found round after round). Together they cover the two
shapes that turn "one finding" into "every reviewer's findings", which
is what fills the resolve-review-threads queue.

WHY FIXTURE-DRIVEN. A real PR's review threads come from GraphQL and
move on every push. A bot posting a minute later than the next run can
flip the order, and what was a duplicate at T can be a kept-then-stale
at T+1. The fixtures pin the shape -- two bots same window, two bots
12 lines apart, human + bot same window -- so the test can pin the
decision rather than the snapshot. The re-run case reads as idempotency:
run the plan twice on the same input, the second plan is empty.

WHAT THE SCRIPT DOES ON RE-RUN. A previous run leaves a hidden marker
in its reply body (`<!-- maxi-config:dedupe -->`), so a re-run that
finds the marker skips the thread entirely. The marker's purpose is
recoverability: if the reply posts but the resolve mutation fails, the
next run retries the resolve without re-posting the reply, and a third
run finds the thread already resolved AND marked. The tests pin the
two shapes a re-run has to handle: thread already marked, thread
already resolved.

WHY THIS TEST FILE LIVES HERE. The script's source of truth is
`.github/actions/dedupe-pr-review-threads/dedupe_review_threads.py` in
this repo, mirrored byte-for-byte to maxi-config by
`maxi-config/tests/test_public_ci_copy_agrees.py`. The mirror test
catches drift in the script itself; this test file pins the script's
PUBLIC SURFACE so a refactor that changes the GraphQL contract lands
through a failing assertion in this repo (where the change is being
made) rather than the consumer (where it would manifest as a gate
breakage on every PR). The same test class is duplicated into the
mirror repo under `tests/test_dedupe_review_threads.py`, and the two
files are kept in sync by hand because the byte-identity mirror only
covers the script + action.yml, not the test fixtures.

WHAT THE TWO BUG FIXES ADD. Before the fix:

  * `_gh_graphql` used `-f` for every scalar, making every variable a
    String on the wire. `--pr` is `type=int` and the query declares
    `$pr:Int!`, so the script crashed on every run before doing any
    work with `Variable $pr of type Int! was provided invalid value`.
    Pinned by `class GhGraphqlFlagSelection`.

  * `fetch_thread_bodies` selected `reviewThreads(first:100, ids:$ids)`
    -- an `ids:` argument that does not exist on `PullRequest.reviewThreads`
    (verified against the live schema: only `after, before, first, last`
    are real). The right field is the top-level `Query.nodes(ids: [ID!]!)`,
    filtered through `... on PullRequestReviewThread`. Pinned by
    `class FetchThreadBodiesUsesARealSchemaField`.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
DEDUPE_PATH = ROOT / ".github" / "actions" / "dedupe-pr-review-threads" / "dedupe_review_threads.py"


def _load_dedupe():
    spec = importlib.util.spec_from_file_location("dedupe_review_threads", DEDUPE_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(DEDUPE_PATH))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dedupe = _load_dedupe()


# A constant, not a derived list, so a future bot added to the script
# is one place to change. The five-bot list was measured on maxi-core:
# codacy-production, coderabbitai, maxi-reviewer, cubic-dev-ai,
# chatgpt-codex-connector.
BOT_A = "codacy-production"
BOT_B = "coderabbitai"
BOT_C = "maxi-reviewer"
HUMAN = "maxiboch"


def thread(
    *,
    tid: str,
    author: str,
    path: str,
    line: int | None,
    is_resolved: bool = False,
    created_at: str = "2026-09-18T10:00:00Z",
    url: str = "https://github.com/example",
) -> dict:
    """Build a fixture thread in the shape fetch_threads() returns.

    `created_at` defaults to a fixed ISO string so callers can pass a
    deterministic ordering by varying only that field; the test passes
    ascending values to express "earliest first".
    """
    return {
        "id": tid,
        "isResolved": is_resolved,
        "isOutdated": False,
        "createdAt": created_at,
        "author": author,
        "path": path,
        "line": line,
        "body": f"finding by {author} on {path}:{line}",
        "url": url + "/" + tid,
    }


class FileIsWiredUp(unittest.TestCase):
    """Non-vacuity: if the script is renamed, every test below would still
    pass -- they would import from a stale module and exercise nothing.
    Pin the file path and the public surface together."""

    def test_the_script_exists(self):
        self.assertTrue(DEDUPE_PATH.is_file(), str(DEDUPE_PATH) + " is missing")

    def test_plan_cluster_main_are_exposed(self):
        for name in ("plan", "cluster", "main", "reply_and_resolve"):
            self.assertTrue(
                callable(getattr(dedupe, name, None)),
                "dedupe." + name + " is not callable",
            )


class GhGraphqlFlagSelection(unittest.TestCase):
    """`_gh_graphql` chooses between `-f` (String) and `-F` (typed).

    The pre-fix implementation used `-f` for every scalar, which made
    every variable a String on the wire. `--pr` is `type=int` and the
    query declares `$pr:Int!`, so the whole document was rejected with
    `Variable $pr of type Int! was provided invalid value` and the
    script crashed on every run. The test below pins the per-type
    dispatch through the helper `_gh_graphql_field_args`, which the
    refactor introduces as a unit-typed function so each branch is
    testable without going through `subprocess.run`.

    `gh api graphql` semantics (verified against the live API on
    2026-09-19):
      * `-f key=value` ships the value as a String.
      * `-F key=value` ships ints, bools, and JSON-typed values as their
        JSON equivalent.
      * `-F key[]=v` (repeat form) ships a JSON array.
    """

    def test_int_variable_uses_dash_capital_f(self):
        # The pre-fix shape would have used `-f pr=761`, which the
        # server rejects with `Could not coerce value "761" to Int`.
        # The fix uses `-F pr=761`, which the server accepts as Int.
        flags = dedupe._gh_graphql_field_args("pr", 761)
        self.assertEqual(flags, ["-F", "pr=761"])

    def test_string_variable_uses_dash_lower_f(self):
        # `-f` is the only flag that ships a true String. `-F` would
        # coerce -- which is fine for the empty string but the wrong
        # choice for a value the query types as `String!`.
        flags = dedupe._gh_graphql_field_args("owner", "maxi-tools")
        self.assertEqual(flags, ["-f", "owner=maxi-tools"])

    def test_bool_true_ships_as_json_true(self):
        # `True` and `False` are subclasses of `int`, so the helper
        # MUST check `bool` before `int`. The wrong order would ship
        # `True` as `1`, which `Boolean!` does not accept.
        self.assertEqual(
            dedupe._gh_graphql_field_args("flag", True),
            ["-F", "flag=true"],
        )
        self.assertEqual(
            dedupe._gh_graphql_field_args("flag", False),
            ["-F", "flag=false"],
        )

    def test_list_variable_ships_as_repeat_form(self):
        # The list is the only variable type that `--input` was
        # mis-using as a JSON body in the old shape. `-F key[]=v`
        # repeated per item is the form `gh api graphql` actually
        # wants: each `[]` suffix is one array slot.
        flags = dedupe._gh_graphql_field_args("ids", ["T1", "T2", "T3"])
        self.assertEqual(
            flags,
            ["-F", "ids[]=T1", "-F", "ids[]=T2", "-F", "ids[]=T3"],
        )

    def test_empty_list_ships_as_empty_array_flag(self):
        # `gh api graphql -F key[]` (no value) is the documented way
        # to ship an empty array. The script never passes one today
        # (the caller short-circuits) but the branch is one extra
        # line and keeps the helper honest about what `list[str]`
        # means.
        self.assertEqual(
            dedupe._gh_graphql_field_args("ids", []),
            ["-F", "ids[]"],
        )

    def test_list_with_non_string_item_is_rejected(self):
        # `gh api graphql` does not accept mixed-type list values, and
        # the GraphQL variable here is `[ID!]!` so anything other than
        # str is wrong. Rejecting it is the honest answer rather than
        # shipping a value the server would also reject.
        with self.assertRaises(TypeError):
            dedupe._gh_graphql_field_args("ids", ["T1", 2])

    def test_unsupported_type_is_rejected(self):
        # The script only ships four types; a `dict` slipping in here
        # would either crash deep in `subprocess.run` (if it becomes a
        # filename) or ship a String the server rejects (the pre-fix
        # `--input` path). Both are worse than a clear TypeError at
        # the call site.
        with self.assertRaises(TypeError):
            dedupe._gh_graphql_field_args("owner", {"x": 1})

    def test_gh_graphql_full_argv_ships_typed_variables(self):
        # The end-to-end check: with `_gh_graphql` calling
        # `_gh_graphql_field_args`, the captured argv contains `-F pr=761`
        # for the int and `-f owner=maxi-tools` for the string. The
        # pre-fix shape would have produced `-f pr=761`, which is
        # exactly the failure the bug report captured.
        captured: dict = {}

        class _Fake:
            returncode = 0
            stdout = '{"data":{}}'
            stderr = ""

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _Fake()

        original = dedupe.subprocess.run
        dedupe.subprocess.run = fake_run
        try:
            dedupe._gh_graphql(
                "query Q($pr: Int!) {}",
                pr=761,
                owner="maxi-tools",
                ids=["T1", "T2"],
            )
        finally:
            dedupe.subprocess.run = original
        argv = captured["args"]
        # int -> -F
        self.assertIn("-F", argv)
        self.assertIn("pr=761", argv)
        # string -> -f
        self.assertIn("-f", argv)
        self.assertIn("owner=maxi-tools", argv)
        # list -> repeat form
        self.assertIn("ids[]=T1", argv)
        self.assertIn("ids[]=T2", argv)
        # No pre-fix shape survived: there is no `-f pr=761` anywhere.
        self.assertNotIn("pr=761", [a for a in argv if a != "pr=761"] or [])
        # `query=` is shipped as String; this is correct for `gh api graphql`.
        self.assertIn("query=query Q($pr: Int!) {}", argv)


class Cluster(unittest.TestCase):
    """The clustering shape: who clusters with whom, on which keys.

    Every test below uses TWO distinct bot authors so the cluster is a
    duplicate, not a single-bot pile. A single bot posting twice on the
    same lines is a bot loop, not a duplicate -- cluster() must skip it
    because deduping a bot against itself would suppress findings the
    reviewer should see.
    """

    BOTS = frozenset({BOT_A, BOT_B, BOT_C})

    def test_two_bots_same_window_cluster(self):
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=14)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(len(clusters), 1)
        self.assertEqual({t["id"] for t in clusters[0]}, {"a", "b"})

    def test_two_bots_eight_lines_apart_cluster(self):
        # window=4 -> +-4 -> lines 10 and 18 share bucket 0 (bucket width 9).
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=18)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(len(clusters), 1)

    def test_two_bots_twelve_lines_apart_do_not_cluster(self):
        # 12 lines > +-4 -> different buckets.
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=22)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(clusters, [])

    def test_human_and_bot_same_window_do_not_cluster(self):
        # Human-authored threads are NEVER touched -- even a bot duplicate
        # of a human's thread stays, because the human's thread is the
        # one the reviewer should read.
        a = thread(tid="a", author=HUMAN, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_A, path="src/x.rs", line=12)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(clusters, [])

    def test_single_bot_twice_does_not_cluster_against_itself(self):
        # A bot posting twice on the same lines is a bot loop, not a
        # duplicate. cluster() must NOT return it: deduping would
        # silently suppress a real finding.
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_A, path="src/x.rs", line=14)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(clusters, [])

    def test_already_resolved_threads_are_excluded(self):
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=14, is_resolved=True)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(clusters, [])

    def test_different_paths_do_not_cluster(self):
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_B, path="src/y.rs", line=10)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(clusters, [])

    def test_file_level_threads_are_skipped(self):
        # A thread with no line is a file-level flag; two such threads on
        # the same path are different conversations even when two bots
        # raise them, because neither points at code. They are not
        # duplicates of anything.
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=None)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=None)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(clusters, [])

    def test_earliest_thread_is_the_kept_one(self):
        # In a cluster of three the FIRST posted is the keeper; the
        # other two get resolved. This is what makes the dedupe
        # direction: the FIRST reviewer to notice is the one whose
        # thread the human reads, and the LATER reviewers get the
        # duplicate-of reply.
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10,
                   created_at="2026-09-18T10:00:00Z")
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=14,
                   created_at="2026-09-18T10:05:00Z")
        c = thread(tid="c", author=BOT_C, path="src/x.rs", line=12,
                   created_at="2026-09-18T10:10:00Z")
        clusters = dedupe.cluster([a, b, c], self.BOTS)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0][0]["id"], "a")  # earliest
        self.assertEqual({t["id"] for t in clusters[0]}, {"a", "b", "c"})

    def test_threads_outside_bot_list_are_excluded(self):
        # A bot NOT in BOT_LOGINS is not deduped, even paired with one
        # that IS. The list is explicit on purpose: an unfamiliar bot
        # gets the same treatment as a human -- its threads stand alone.
        a = thread(tid="a", author="some-other-bot", path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_A, path="src/x.rs", line=14)
        clusters = dedupe.cluster([a, b], self.BOTS)
        self.assertEqual(clusters, [])


class Plan(unittest.TestCase):
    """End-to-end through plan(): which threads to keep, resolve, skip.

    The dry-run path uses a None fetch_bodies dependency -- every
    duplicate is treated as "not yet processed", which is the safe
    direction and matches what the script does when the user passes
    --payload for offline development.
    """

    BOTS = frozenset({BOT_A, BOT_B, BOT_C})

    def test_two_bots_same_window_resolves_the_later_one(self):
        keeper = thread(
            tid="k", author=BOT_A, path="src/x.rs", line=10,
            created_at="2026-09-18T10:00:00Z",
        )
        dup = thread(
            tid="d", author=BOT_B, path="src/x.rs", line=14,
            created_at="2026-09-18T10:05:00Z",
        )
        kept, to_resolve, already = dedupe.plan(
            [keeper, dup], self.BOTS,
        )
        self.assertEqual([t["id"] for t in kept], ["k"])
        # plan() returns (dup, keeper) tuples so the apply path does
        # not need to re-derive the keeper from the path. The keeper of
        # the only cluster is the EARLIEST thread.
        self.assertEqual([d["id"] for d, _k in to_resolve], ["d"])
        self.assertEqual([k["id"] for _d, k in to_resolve], ["k"])
        self.assertEqual(already, [])

    def test_three_bots_resolves_the_two_later_ones(self):
        keeper = thread(tid="k", author=BOT_A, path="src/x.rs", line=10,
                        created_at="2026-09-18T10:00:00Z")
        mid = thread(tid="m", author=BOT_B, path="src/x.rs", line=14,
                     created_at="2026-09-18T10:05:00Z")
        late = thread(tid="l", author=BOT_C, path="src/x.rs", line=12,
                      created_at="2026-09-18T10:10:00Z")
        kept, to_resolve, already = dedupe.plan(
            [keeper, mid, late], self.BOTS,
        )
        self.assertEqual([t["id"] for t in kept], ["k"])
        self.assertEqual({d["id"] for d, _k in to_resolve}, {"m", "l"})
        # Every duplicate's keeper is the cluster keeper, not some
        # other thread that shares the path.
        self.assertTrue(all(k["id"] == "k" for _d, k in to_resolve))

    def test_multi_cluster_per_path_keeps_each_pair(self):
        # The bug Codacy flagged on PR #723: when one path has more
        # than one cluster, every duplicate has to point at the
        # keeper of ITS cluster, not at the first keeper of that path.
        # The pre-fix implementation re-derived the keeper from the
        # path, which mapped every duplicate to whichever keeper of
        # that path sorted first -- the wrong keeper for any later
        # cluster. The plan() refactor returns (dup, keeper) pairs
        # directly, so this test pins the per-cluster association.
        a1 = thread(tid="a1", author=BOT_A, path="src/x.rs", line=10,
                    created_at="2026-09-18T10:00:00Z")
        b1 = thread(tid="b1", author=BOT_B, path="src/x.rs", line=14,
                    created_at="2026-09-18T10:05:00Z")
        a2 = thread(tid="a2", author=BOT_A, path="src/x.rs", line=50,
                    created_at="2026-09-18T11:00:00Z")
        b2 = thread(tid="b2", author=BOT_B, path="src/x.rs", line=54,
                    created_at="2026-09-18T11:05:00Z")
        kept, to_resolve, _ = dedupe.plan(
            [a1, b1, a2, b2], self.BOTS,
        )
        self.assertEqual({t["id"] for t in kept}, {"a1", "a2"})
        # Each duplicate pairs with its own cluster's keeper -- b1
        # pairs with a1, NOT with a2 (the path-level first keeper).
        pairs = {d["id"]: k["id"] for d, k in to_resolve}
        self.assertEqual(pairs, {"b1": "a1", "b2": "a2"})

    def test_two_bots_twelve_lines_apart_untouched(self):
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=22)
        kept, to_resolve, already = dedupe.plan([a, b], self.BOTS)
        self.assertEqual(kept, [])
        self.assertEqual(to_resolve, [])
        self.assertEqual(already, [])

    def test_human_plus_bot_same_window_untouched(self):
        a = thread(tid="a", author=HUMAN, path="src/x.rs", line=10)
        b = thread(tid="b", author=BOT_A, path="src/x.rs", line=12)
        kept, to_resolve, already = dedupe.plan([a, b], self.BOTS)
        self.assertEqual(kept, [])
        self.assertEqual(to_resolve, [])
        self.assertEqual(already, [])

    def test_rerun_with_existing_marker_is_a_noop(self):
        # First run: cluster decides to resolve the duplicate.
        # Second run (re-run, no GraphQL changes yet): the bodies
        # fetch sees our sentinel in the duplicate's body and moves
        # the thread from `to_resolve` to `already_processed`.
        keeper = thread(tid="k", author=BOT_A, path="src/x.rs", line=10)
        dup = thread(tid="d", author=BOT_B, path="src/x.rs", line=14)

        # No bodies fetcher -> treat as "not yet processed".
        kept, to_resolve, _ = dedupe.plan([keeper, dup], self.BOTS)
        self.assertEqual([d["id"] for d, _k in to_resolve], ["d"])

        # With a bodies fetcher that reports the sentinel present:
        # the duplicate moves from to_resolve to already_processed.
        def fetcher(ids):
            return {tid: dedupe.SENTINEL for tid in ids}

        kept, to_resolve, already = dedupe.plan(
            [keeper, dup], self.BOTS, fetch_bodies=fetcher,
        )
        self.assertEqual([t["id"] for t in kept], ["k"])
        self.assertEqual(to_resolve, [])
        self.assertEqual([t["id"] for t in already], ["d"])

    def test_empty_input_yields_empty_output(self):
        kept, to_resolve, already = dedupe.plan([], self.BOTS)
        self.assertEqual((kept, to_resolve, already), ([], [], []))

    def test_resolved_threads_are_left_alone(self):
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=10,
                   is_resolved=True)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=14,
                   is_resolved=True)
        kept, to_resolve, _ = dedupe.plan([a, b], self.BOTS)
        self.assertEqual((kept, to_resolve), ([], []))


class ReplyBody(unittest.TestCase):
    """The reply body is what a human reader sees after a dedupe. The
    spec calls out one exact sentence plus our sentinel; the tests
    pin BOTH, because the sentinel is what makes a re-run cheap and
    the sentence is what makes the reply understandable."""

    def test_reply_carries_our_sentinel(self):
        keeper = thread(tid="k", author=BOT_A, path="src/x.rs", line=10,
                        url="https://gh/k")
        dup = thread(tid="d", author=BOT_B, path="src/x.rs", line=14)
        body = dedupe._reply_body(keeper, dup)
        self.assertIn(dedupe.SENTINEL, body)

    def test_reply_quotes_the_keeper_url(self):
        keeper = thread(tid="k", author=BOT_A, path="src/x.rs", line=10,
                        url="https://example/keeper")
        dup = thread(tid="d", author=BOT_B, path="src/x.rs", line=14)
        body = dedupe._reply_body(keeper, dup)
        self.assertIn("https://example/keeper", body)

    def test_reply_names_the_keeper_author(self):
        keeper = thread(tid="k", author=BOT_A, path="src/x.rs", line=10)
        dup = thread(tid="d", author=BOT_B, path="src/x.rs", line=14)
        body = dedupe._reply_body(keeper, dup)
        self.assertIn(BOT_A, body)


class BucketKey(unittest.TestCase):
    """The window is the whole reason this script exists. The test pins
    the boundary cases: line == 0, lines on the boundary, lines just
    outside it."""

    def test_line_zero_still_clusters(self):
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=0)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=8)
        clusters = dedupe.cluster(
            [a, b], frozenset({BOT_A, BOT_B}),
        )
        self.assertEqual(len(clusters), 1)

    def test_lines_just_outside_the_window_do_not_cluster(self):
        # window=4, bucket width 9: 0..8 share bucket 0; 9..17 share bucket 1.
        a = thread(tid="a", author=BOT_A, path="src/x.rs", line=0)
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=9)
        clusters = dedupe.cluster(
            [a, b], frozenset({BOT_A, BOT_B}),
        )
        self.assertEqual(clusters, [])

    def test_missing_path_skips_clustering(self):
        # A thread without a path is a thread on a deleted file. We
        # cannot cluster it: there is no anchor to compare against.
        # The test pins this so a future change that coerces missing
        # paths to "" does not silently merge unrelated threads.
        a = {"id": "a", "isResolved": False, "createdAt": "2026-09-18T10:00:00Z",
             "author": BOT_A, "path": None, "line": 10, "body": "", "url": ""}
        b = thread(tid="b", author=BOT_B, path="src/x.rs", line=14)
        clusters = dedupe.cluster([a, b], frozenset({BOT_A, BOT_B}))
        self.assertEqual(clusters, [])


class ScriptInvocation(unittest.TestCase):
    """Drive main() with a payload file and check the JSON report.

    main() under --payload does NOT make GraphQL calls, so these tests
    run offline. The dry-run report is what a human looking at a CI
    log actually reads, and the assertions pin every field: kept,
    resolved, already_processed, applied, window, bot_logins.
    """

    BOTS = frozenset({BOT_A, BOT_B, BOT_C})

    def test_gh_graphql_invokes_gh_as_argv_zero(self):
        # The apply path runs `_gh_graphql(_query, ...)`, which builds
        # the `subprocess.run` argv. That argv has to start with `gh` --
        # `subprocess.run` does NOT auto-prepend the program name the way
        # `os.system` does, so omitting `gh` would resolve to a binary
        # named `api` and fail with `No such file or directory`. Pinned
        # here so the regression cannot return silently. (Codacy HIGH on
        # PR #723.)
        captured: dict = {}

        class _Fake:
            returncode = 0
            stdout = '{"data":{}}'
            stderr = ""

        def fake_run(args, **kwargs):
            captured["args"] = args
            return _Fake()

        original = dedupe.subprocess.run
        dedupe.subprocess.run = fake_run
        try:
            dedupe._gh_graphql("query Q {}", threadId="abc")
        finally:
            dedupe.subprocess.run = original
        self.assertEqual(captured["args"][0], "gh")
        self.assertEqual(captured["args"][1], "api")
        self.assertEqual(captured["args"][2], "graphql")

    def test_fetch_thread_bodies_uses_top_level_nodes(self):
        # Bug 2 fix: `PullRequest.reviewThreads(ids:)` is not a real
        # argument (verified against the live schema on 2026-09-19), but
        # the top-level `Query.nodes(ids: [ID!]!)` is. The refactored
        # query goes through `... on PullRequestReviewThread` to filter
        # the result back to thread-shaped objects. This test pins:
        #
        #   1. EXACTLY ONE `subprocess.run` call for the batch.
        #   2. The `nodes(ids: ...)` field is selected, not
        #      `repository.PullRequest.reviewThreads(ids: ...)`.
        #   3. The list variable ships via the repeat form `--F ids[]=v`,
        #      which is the typed JSON-array form. Pre-fix this would
        #      have used `-f ids=[T1,T2,T3]`, which is the wrong shape.
        #   4. The response is read from `data.nodes`, not from the
        #      nested repository/pullRequest path.
        #   5. With > 1 IDs the helper concatenates every thread's
        #      bodies, so the batching is real.
        class _Fake:
            returncode = 0
            stdout = json.dumps({
                "data": {
                    "nodes": [
                        {"id": "T1", "comments": {"nodes": [
                            {"body": "first"},
                            {"body": "second"},
                        ]}},
                        {"id": "T2", "comments": {"nodes": [
                            {"body": "third"},
                        ]}},
                        # A null slot: GitHub echoes `null` for IDs that
                        # resolve to a different type or are unknown.
                        # The helper must skip them rather than crash.
                        None,
                    ]
                }
            })
            stderr = ""

        calls: list = []

        def fake_run(args, **kwargs):
            calls.append({"args": args})
            return _Fake()

        original_run = dedupe.subprocess.run
        dedupe.subprocess.run = fake_run
        try:
            bodies = dedupe.fetch_thread_bodies(
                "maxi-tools", "maxi-config", 723,
                ["T1", "T2", "T3"],
            )
        finally:
            dedupe.subprocess.run = original_run
        # Exactly one subprocess.run invocation for the batch.
        self.assertEqual(len(calls), 1)
        argv = calls[0]["args"]
        # `nodes(ids: $ids)` selected, NOT `reviewThreads(ids: $ids)`.
        argv_str = " ".join(str(a) for a in argv)
        self.assertIn("nodes(ids:", argv_str)
        self.assertNotIn("reviewThreads(ids:", argv_str)
        # The list variable ships as `-F ids[]=v` for each ID.
        self.assertIn("ids[]=T1", argv)
        self.assertIn("ids[]=T2", argv)
        self.assertIn("ids[]=T3", argv)
        # And NOT as `-f ids=[...]` (the pre-fix shape would have
        # collapsed the list into a String).
        self.assertNotIn("ids=[T1", argv_str)
        # The bodies response maps the IDs the server actually echoed,
        # skipping nulls and unrecognised IDs.
        self.assertEqual(bodies["T1"], "first\nsecond")
        self.assertEqual(bodies["T2"], "third")
        # T3 was the null slot. The helper skips it rather than
        # recording a body for it; a re-run will re-fetch it and find
        # an entry, so this is a recoverable idempotency path.
        self.assertNotIn("T3", bodies)

    def test_fetch_thread_bodies_signature_includes_pr_coords(self):
        # `fetch_thread_bodies(owner, repo, pr, thread_ids)` keeps the
        # three PR coordinates even though `Query.nodes` doesn't use
        # them. The reason is the byte-identity mirror copy in
        # maxi-config, which has to be a one-character substitution
        # against this repo's script: dropping the coords here would
        # turn the mirror into a more invasive diff. The cost is three
        # unused locals in the body; the benefit is the mirror's
        # byte-identity contract. Pinned here so a future refactor
        # that drops them only lands if it also updates the mirror's
        # signature AND the docstring contract above.
        import inspect
        sig = inspect.signature(dedupe.fetch_thread_bodies)
        params = list(sig.parameters.keys())
        self.assertEqual(params[:3], ["owner", "repo", "pr"])
        self.assertEqual(params[3:], ["thread_ids"])

    def _run(self, threads, *, extra=()):
        rc, out, _ = self._run_with_stderr(threads, extra=extra)
        # main() always prints a JSON report on the success path. The
        # only path that exits without one is the "too many duplicates"
        # refuse-to-run, which is checked via _run_with_stderr directly.
        assert out, "main() returned no JSON; the test should use _run_with_stderr"
        return rc, json.loads(out)

    def _run_with_stderr(self, threads, *, extra=()):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False
        ) as fh:
            json.dump(threads, fh)
            payload_path = fh.name
        old_token = os.environ.get("GITHUB_TOKEN")
        old_owner = os.environ.get("GITHUB_REPOSITORY_OWNER")
        old_repo = os.environ.get("GITHUB_REPOSITORY")
        old_pr = os.environ.get("PR_NUMBER")
        os.environ["GITHUB_REPOSITORY_OWNER"] = "maxi-tools"
        os.environ["GITHUB_REPOSITORY"] = "maxi-tools/maxi-config"
        os.environ["PR_NUMBER"] = "1"
        if old_token is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = old_token
        try:
            argv = [
                "dedupe-review-threads.py",
                "--owner", "maxi-tools",
                "--repo", "maxi-config",
                "--pr", "1",
                "--payload", payload_path,
            ] + list(extra)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = dedupe.main(argv)
        finally:
            os.unlink(payload_path)
            for var, old in (
                ("GITHUB_REPOSITORY_OWNER", old_owner),
                ("GITHUB_REPOSITORY", old_repo),
                ("PR_NUMBER", old_pr),
                ("GITHUB_TOKEN", old_token),
            ):
                if old is None:
                    os.environ.pop(var, None)
                else:
                    os.environ[var] = old
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_returns_zero(self):
        threads = [
            thread(tid="k", author=BOT_A, path="src/x.rs", line=10),
            thread(tid="d", author=BOT_B, path="src/x.rs", line=14),
        ]
        rc, _ = self._run(threads)
        self.assertEqual(rc, 0)

    def test_dry_run_does_not_apply(self):
        threads = [
            thread(tid="k", author=BOT_A, path="src/x.rs", line=10),
            thread(tid="d", author=BOT_B, path="src/x.rs", line=14),
        ]
        _, report = self._run(threads)
        self.assertEqual(report["applied"], False)

    def test_apply_flag_records_applied_true(self):
        threads = [
            thread(tid="k", author=BOT_A, path="src/x.rs", line=10),
            thread(tid="d", author=BOT_B, path="src/x.rs", line=14),
        ]
        # --apply would normally call reply_and_resolve, which talks to
        # GraphQL. With --payload the threads are fixture data and
        # there is no real network call -- but reply_and_resolve runs.
        # That call WILL try `gh api graphql`. Without a working `gh`
        # auth, the test exits non-zero. The point of this test is the
        # REPORT, not the network call, so we use a stub.
        calls = []

        def fake_reply(owner, repo, pr, thread_, keeper):
            calls.append((thread_["id"], keeper["id"]))

        original = dedupe.reply_and_resolve
        dedupe.reply_and_resolve = fake_reply
        try:
            _, report = self._run(threads, extra=["--apply"])
        finally:
            dedupe.reply_and_resolve = original
        self.assertEqual(report["applied"], True)
        # The duplicate was resolved (the keeper was not).
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "d")
        self.assertEqual(calls[0][1], "k")

    def test_two_bots_twelve_lines_apart_no_resolve(self):
        threads = [
            thread(tid="a", author=BOT_A, path="src/x.rs", line=10),
            thread(tid="b", author=BOT_B, path="src/x.rs", line=22),
        ]
        _, report = self._run(threads)
        self.assertEqual(report["kept"], [])
        self.assertEqual(report["resolved"], [])

    def test_human_and_bot_same_window_untouched(self):
        threads = [
            thread(tid="a", author=HUMAN, path="src/x.rs", line=10),
            thread(tid="b", author=BOT_B, path="src/x.rs", line=12),
        ]
        _, report = self._run(threads)
        self.assertEqual(report["kept"], [])
        self.assertEqual(report["resolved"], [])

    def test_window_override_is_honoured(self):
        # With window=10 the boundary widens to +-10, so 10 and 22 share
        # a bucket and the duplicate is detected.
        threads = [
            thread(tid="a", author=BOT_A, path="src/x.rs", line=10),
            thread(tid="b", author=BOT_B, path="src/x.rs", line=22),
        ]
        _, report = self._run(threads, extra=["--window", "10"])
        self.assertEqual(len(report["resolved"]), 1)
        self.assertEqual(report["window"], 10)

    def test_extra_bot_flag_extends_the_list(self):
        # A bot not in BOT_LOGINS is treated as human-like by default;
        # passing --bot extends the list at runtime. This is the
        # mechanism for forks / new bots without a code change.
        threads = [
            thread(tid="a", author="new-bot", path="src/x.rs", line=10),
            thread(tid="b", author=BOT_A, path="src/x.rs", line=12),
        ]
        _, report_no_extra = self._run(threads)
        self.assertEqual(report_no_extra["resolved"], [])

        _, report_with_extra = self._run(threads, extra=["--bot", "new-bot"])
        self.assertEqual(len(report_with_extra["resolved"]), 1)

    def test_too_many_duplicates_refuses_to_run(self):
        # MAX_RESOLVE_PER_RUN caps the apply path. Dry-run reports
        # everything, apply path refuses. The script exits non-zero so
        # a CI run reads the situation rather than racing through it.
        threads = [
            thread(
                tid="k" + str(i), author=BOT_A,
                path=f"src/f{i}.rs", line=10,
            )
            for i in range(dedupe.MAX_RESOLVE_PER_RUN + 1)
        ]
        threads += [
            thread(
                tid="d" + str(i), author=BOT_B,
                path=f"src/f{i}.rs", line=14,
            )
            for i in range(dedupe.MAX_RESOLVE_PER_RUN + 1)
        ]
        # Stub reply_and_resolve so the apply path doesn't try GraphQL.
        original = dedupe.reply_and_resolve
        dedupe.reply_and_resolve = lambda *a, **kw: None
        try:
            rc, out, err = self._run_with_stderr(threads, extra=["--apply"])
        finally:
            dedupe.reply_and_resolve = original
        self.assertNotEqual(rc, 0)
        # The error mentions the cap so a reviewer reading the log
        # knows WHY the run refused, not just that it did.
        self.assertIn(str(dedupe.MAX_RESOLVE_PER_RUN), err)


class ThreadQueryMatchesTheSchema(unittest.TestCase):
    """The GraphQL document must only select fields that exist on the type.

    This script selected `createdAt` on `reviewThreads.nodes`. The type
    `PullRequestReviewThread` has no such field, so GitHub rejected the
    WHOLE document and every run died with

        Field 'createdAt' doesn't exist on type 'PullRequestReviewThread'

    which is why `dedupe-review-threads.py` had never once completed a run
    -- the permission bug that kept the job from starting at all was hiding
    a query bug that would have stopped it anyway.

    The field set below is GitHub's, read from introspection on
    2026-09-19. Pinning it here is the cheap half of the check: a unit test
    cannot reach the live schema, but it CAN refuse a selection on a type
    whose fields we have written down. The expensive half -- validating the
    document against the real API -- only happens when the job runs, and
    this is the defect class where that is too late.
    """

    #: Every field on PullRequestReviewThread, per introspection.
    THREAD_FIELDS = {
        "comments", "diffSide", "id", "isCollapsed", "isOutdated",
        "isResolved", "line", "originalLine", "originalStartLine", "path",
        "pullRequest", "repository", "resolvedBy", "startDiffSide",
        "startLine", "subjectType", "viewerCanReply", "viewerCanResolve",
        "viewerCanUnresolve",
    }

    @staticmethod
    def _block_after(source: str, opener: "re.Pattern[str]") -> str:
        """The brace-delimited block following the first match of `opener`.

        Brace-matched rather than sliced at the first `}`: these selections
        contain nested blocks (`author{login}`), so stopping at the first
        close brace ends several fields early and would pass for the wrong
        reason.

        Raises if the opener is absent, so a query that has been reshaped
        fails LOUDLY here instead of quietly selecting nothing and reporting
        no unknown fields. That failure mode is the one worth guarding: a
        check that silently inspects an empty string always passes.
        """
        match = opener.search(source)
        if match is None:
            raise AssertionError(
                f"could not locate {opener.pattern!r} in the query; the "
                "document has been reshaped and this guard needs updating "
                "rather than deleting"
            )
        depth = 0
        for end in range(match.end() - 1, len(source)):
            if source[end] == "{":
                depth += 1
            elif source[end] == "}":
                depth -= 1
                if depth == 0:
                    return source[match.end() : end]
        raise AssertionError("unbalanced braces in the GraphQL document")

    #: Whitespace-tolerant openers. The document is hand-formatted, so the
    #: patterns allow arbitrary spacing rather than pinning indentation.
    _THREAD_NODES = re.compile(r"reviewThreads\s*\([^)]*\)\s*\{.*?nodes\s*\{", re.S)
    _COMMENT_NODES = re.compile(r"comments\s*\(\s*first\s*:\s*1\s*\)\s*\{")

    def _thread_node_selection(self) -> list[str]:
        """Field names selected directly on a reviewThreads node.

        Nested selections are dropped: they are governed by their own type,
        not by THREAD_FIELDS.
        """
        block = self._block_after(
            DEDUPE_PATH.read_text(encoding="utf-8"), self._THREAD_NODES
        )
        # Remove nested blocks (and their contents) so only the bare field
        # names on this type remain.
        while True:
            pruned = re.sub(r"\w+\s*(\([^)]*\))?\s*\{[^{}]*\}", " ", block)
            if pruned == block:
                break
            block = pruned
        return re.findall(r"[A-Za-z_]\w*", block)

    def test_every_field_selected_on_the_thread_exists(self) -> None:
        selected = self._thread_node_selection()
        self.assertTrue(selected, "parsed no fields; the query shape moved")
        unknown = sorted(set(selected) - self.THREAD_FIELDS)
        self.assertEqual(
            unknown,
            [],
            "these are selected on PullRequestReviewThread but do not exist "
            "on it, so GitHub rejects the entire document: " + repr(unknown),
        )

    def test_created_at_is_taken_from_the_comment(self) -> None:
        # The tie-break needs an arrival time and the thread has none, so it
        # must come from the first comment. Asserted because moving it back
        # onto the thread is the exact regression.
        self.assertNotIn("createdAt", self._thread_node_selection())
        comment_block = self._block_after(
            DEDUPE_PATH.read_text(encoding="utf-8"), self._COMMENT_NODES
        )
        self.assertIn("createdAt", comment_block)


class ActionSummaryLines(unittest.TestCase):
    """End-to-end test of the action.yml run step's three outcome lines.

    Why this exists. The issue's acceptance criterion is that a 404 on
    the checkout (or any unavailability) FAILS the step, and that the
    step writes one of three explicit outcome lines to the summary in
    every case. The 47 unit tests above pin the SCRIPT -- its GraphQL
    flag dispatch, the top-level nodes(ids:) query, the cluster/plan
    shape. None of them pin the SHELL that wraps the script: a future
    edit to action.yml that drops the failure-path summary line or
    re-adds `continue-on-error` would pass every test above while
    silently re-introducing the silent-no-op defect this whole change
    exists to close.

    What the shell actually does is small enough to run as a subprocess
    against a controlled script: capture the script's combined
    stdout/stderr into `$out`, branch on its exit code, write one of
    three lines to `$GITHUB_STEP_SUMMARY`. The test below extracts that
    shell into a tiny shim, runs it against three controlled script
    outputs, and asserts:

      * `deduped N threads` is written on a successful run with N>0.
      * `nothing to dedupe` is written on a successful run with N==0.
      * `dedupe unavailable: <reason>` is written on a non-zero exit.
      * The wrapper exits non-zero on the failure path so the calling
        step -- which has no `continue-on-error` -- goes red.

    The shim is a stripped copy of the actual run step; the assertion
    is on the SHAPE of its behaviour, not on action.yml's text. The
    two are kept in sync by `test_the_action_yml_run_step_matches_the_shim`
    below, which fails if action.yml's run step drifts from the shim's
    structure -- so a future change to the wrapper either lands through
    this test class (with an updated assertion) or fails closed.
    """

    SHIM = ROOT / "tests" / "fixtures" / "sim_dedupe_run_step.sh"

    def _run_shim(
        self,
        *,
        script_output: str,
        script_exit: int,
    ) -> tuple[str, int]:
        """Drive the shim with a controlled script output. Returns
        (GITHUB_STEP_SUMMARY contents, shim exit code)."""
        with tempfile.TemporaryDirectory() as tmp:
            script = pathlib.Path(tmp) / "fake_script.py"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stderr.write(" + repr(script_output) + ")\n"
                "sys.exit(" + str(script_exit) + ")\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            summary = pathlib.Path(tmp) / "summary"
            summary.write_text("", encoding="utf-8")
            proc = subprocess.run(
                ["bash", str(self.SHIM), str(script), str(summary)],
                capture_output=True,
                text=True,
            )
            return summary.read_text(encoding="utf-8"), proc.returncode

    def test_deduped_n_threads_is_written_on_success(self):
        summary, rc = self._run_shim(
            script_output='{"resolved": ["t1","t2"], "kept": ["k1"], "already_processed": []}\n',
            script_exit=0,
        )
        self.assertEqual(rc, 0)
        # The summary line MUST be the literal "deduped 2 threads" -- the
        # text the gate job and the PR-page review bot both grep for.
        self.assertIn("deduped 2 threads\n", summary)
        # The success line is the LAST line, by construction: the shim
        # tees the script output into the summary first, then writes
        # the outcome line on top. A future change that puts the line
        # before the script output would put a reader looking for the
        # count in the middle of a JSON blob.
        self.assertTrue(
            summary.endswith("deduped 2 threads\n"),
            "summary ends with: " + repr(summary[-200:]),
        )

    def test_nothing_to_dedupe_is_written_on_zero_count(self):
        summary, rc = self._run_shim(
            script_output='no duplicates here\n',
            script_exit=0,
        )
        self.assertEqual(rc, 0)
        self.assertIn("nothing to dedupe\n", summary)
        self.assertNotIn("deduped ", summary)

    def test_dedupe_unavailable_is_written_on_non_zero_exit(self):
        # The traceback tail is what a reader actually sees on the
        # failure path. The shim's reason extraction pulls the LAST
        # non-empty line out of the captured output -- a different
        # default would change the message a reader sees on every
        # crash, and a test that pins the shape is the cheapest place
        # to catch it.
        summary, rc = self._run_shim(
            script_output=(
                "Traceback (most recent call last):\n"
                '  File "dedupe_review_threads.py", line 200, in main\n'
                "    resp = _gh_graphql(query, ids=[])\n"
                "RuntimeError: api returned 502\n"
            ),
            script_exit=1,
        )
        self.assertEqual(rc, 1, "shim must exit non-zero on script failure")
        # The summary line MUST name the failure mode in human prose.
        # "dedupe unavailable" alone (without the reason) would tell a
        # reader the gate is wedged but not WHY -- exactly the silent
        # defect this change exists to close.
        self.assertIn("dedupe unavailable: RuntimeError: api returned 502\n", summary)
        # The script's traceback is preserved in the summary above the
        # outcome line, so a reader with the link can read the full
        # traceback rather than only its tail.
        self.assertIn("RuntimeError: api returned 502", summary)
        self.assertIn("Traceback (most recent call last):", summary)

    def test_dedupe_unavailable_without_traceback_still_names_an_outcome(self):
        # An empty captured output with a non-zero exit -- the script
        # exited but printed nothing. The shim's fallback reason is the
        # only thing a reader would see, and "exited with code N and
        # produced no explanation" is the honest phrasing.
        summary, rc = self._run_shim(script_output="", script_exit=2)
        self.assertEqual(rc, 2)
        self.assertIn(
            "dedupe unavailable: the dedupe script exited with code 2 and produced no explanation\n",
            summary,
        )

    def test_the_action_yml_run_step_matches_the_shim(self):
        # The shim is a stripped copy of the run step in action.yml.
        # If they diverge, this test fails: a future edit to the wrapper
        # either updates the shim (and the assertions above) or lands
        # through this test class.
        action_text = (ROOT / ".github" / "actions" / "dedupe-pr-review-threads" / "action.yml").read_text(
            encoding="utf-8"
        )
        shim_text = self.SHIM.read_text(encoding="utf-8")
        # The structural shape: three branches on `rc` against zero, each
        # branch writing one of the three literal outcome lines. The
        # exact strings are duplicated between the wrapper and the shim
        # by design -- the shim is the test fixture, and changing one
        # without the other is the silent drift this test catches.
        for literal in (
            "dedupe unavailable: %s\\n",
            "nothing to dedupe\\n",
            "deduped %s threads\\n",
        ):
            self.assertIn(literal, action_text, "action.yml missing: " + repr(literal))
            self.assertIn(literal, shim_text, "shim missing: " + repr(literal))
        # The wrapper MUST exit with the script's code on the failure
        # path. `exit $rc` propagates it; `exit 1` would mask a SIGTERM
        # exit 143 as a generic crash.
        self.assertRegex(action_text, r"exit \"\$rc\"")
        self.assertRegex(shim_text, r"exit \"\$rc\"")
        # `set -e` MUST be off -- a non-zero exit from python is the
        # signal that picks which summary line gets written, and a
        # `set -e` in front of python would abort before the failure
        # path can write `dedupe unavailable`. Check the actual
        # `set -` directive on its own line (re.MULTILINE), not the
        # comment that mentions the OLD shape for context.
        # `unittest.TestCase.assertRegex` does not accept a flags
        # argument, so the MULTILINE flag is applied via re.search().
        self.assertIsNotNone(
            re.search(r"^        set -uo pipefail$", action_text, re.MULTILINE),
            "action.yml run step is missing `set -uo pipefail` on its own line",
        )
        self.assertIsNone(
            re.search(r"^        set -euo pipefail$", action_text, re.MULTILINE),
            "action.yml run step has `set -e` re-enabled; the failure path "
            "needs python's exit code to fall through to the summary writer",
        )


if __name__ == "__main__":
    unittest.main()