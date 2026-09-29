#!/usr/bin/env python3
"""An approval of the CURRENT head is a second way past condition 2.

Maxi decision 2026-09-28: bots count if they approve. Concretely, an
APPROVED review of the pull request's current head satisfies
`review-gate/non-author-review` when its author is either

  * a review bot on the trusted allowlist (`TRUSTED_REVIEW_BOTS` in the
    gate -- one list, one place), or
  * any account that is not a bot and is not the pull request's author.

Four cases are the decision's own acceptance list, and they are the first
four tests below: an allowlisted bot APPROVE at head passes; a bot
COMMENTED review fails; the author's own approval fails; an approval at a
stale head fails.

WHY THE FOURTH ONE IS NEW. The gate used to read `state` and ignore the
commit the review was submitted against, so an approval -- or any other
counted review -- of a superseded revision carried the same weight as one
of the code that would merge. The approval path compares `commit` against
the payload's `headSha` and credits nothing else.

WHY A COMMENTED BOT REVIEW CAN STILL PASS, WHICH IS NOT A CONTRADICTION.
`test_a_roster_asked_reviewer_still_satisfies_a_commented_review` is the
pre-existing rule and it is deliberately kept: a review bot's COMMENTED
review from a reviewer the roster ASKED for satisfies the condition,
exactly as it did before this change. The reason is measured rather than
preferred. Across 604 open pull requests in 19 repositories on
2026-09-28 the review states are COMMENTED 3001, CHANGES_REQUESTED 5,
DISMISSED 4, APPROVED 0: no reviewer in this org submits an approval, so a
condition satisfied only by an approval would be red on every pull request
in the fleet -- the opposite of what the decision is for. "A bot COMMENTED
review does not count" is therefore implemented as what an approval IS: a
comment is not an approval, so it never satisfies the approval path, and
the allowlist does not turn it into one. What it keeps doing is satisfying
the roster path, which is the assignment, not an approval.

Run directly: `python3 tests/test_trusted_bot_approval.py`.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
GATE = ROOT / ".github/actions/pr-review-gate/pr_review_gate.py"
COLLECTOR = ROOT / ".github/actions/collect-pr-review-state/action.yml"

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
AUTHOR = "maxiboch"


def _load_gate():
    spec = importlib.util.spec_from_file_location("pr_review_gate", GATE)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load " + str(GATE))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

# The roster the selector publishes for an ordinary pull request: reviewers
# the org runs, none of which is `coderabbitai`'s own spelling problem.
ASKED = ["maxi-reviewer", "coderabbit"]


def review(author, state, *, commit=HEAD, is_bot=False):
    """One collected review, in the shape the collector writes."""
    return {"author": author, "state": state, "commit": commit, "isBot": is_bot}


def payload(reviews, *, head=HEAD, roster=None, author=AUTHOR):
    doc = {
        "author": author,
        "isDraft": False,
        "headRefName": "wt/trusted-bot-approval",
        "labels": [],
        "headSha": head,
        "threads": [],
        "reviews": list(reviews),
    }
    if roster is not None:
        doc["roster"] = roster
    return doc


def roster(asked=ASKED, skipped=0):
    return {"asked": list(asked), "skipped": skipped}


def verdict(doc):
    ok, lines = gate.evaluate(doc, only=gate.ONLY_REVIEWER)
    return ok, "\n".join(lines)


class TheDecisionCases(unittest.TestCase):
    """The four assertions in the card, in order."""

    def test_an_allowlisted_bot_approving_this_head_satisfies_the_gate(self):
        # The roster asked for maxi-reviewer and coderabbit; the approval
        # comes from cubic. It counts anyway: the allowlist is the trust
        # decision for a bot, so the gate does not depend on the selector
        # having spelled this reviewer's name.
        ok, text = verdict(
            payload(
                [review("cubic-dev-ai", "APPROVED", is_bot=True)],
                roster=roster(),
            )
        )
        self.assertTrue(ok, text)
        self.assertIn("cubic-dev-ai", text)
        self.assertIn("trusted review bot", text)

    def test_a_bot_commented_review_does_not_satisfy_the_gate(self):
        # A COMMENTED review is not an approval. Nothing in it says the bot
        # is done and happy, which is the only thing an approval can mean,
        # and the allowlist does not turn a comment into one.
        #
        # The reviewer here is on the allowlist and the roster did not ask
        # for it, which is the case this decision is about: a bot commenting
        # is not a bot approving, and being a trusted reviewer does not by
        # itself entitle it to satisfy a condition it was not assigned.
        ok, text = verdict(
            payload(
                [review("cubic-dev-ai", "COMMENTED", is_bot=True)],
                roster=roster(),
            )
        )
        self.assertFalse(ok, text)
        self.assertIn("FAIL", text)

    def test_the_authors_own_approval_does_not_satisfy_the_gate(self):
        # Every agent lane in this org authenticates as the same account, so
        # self-review is the common case rather than an edge one.
        ok, text = verdict(
            payload(
                [review(AUTHOR, "APPROVED", is_bot=False)],
            )
        )
        self.assertFalse(ok, text)
        self.assertIn("no review from anyone other than the author", text)

    def test_an_approval_at_a_stale_head_does_not_satisfy_the_gate(self):
        # The bot approved the PREVIOUS revision: its approval is of code
        # that is no longer in the pull request.
        #
        # The reviewer is the one the roster ASKED for, which is the hard
        # case. An approval belongs to the approval route and never to the
        # roster route, so being assigned does not rescue it: the roster
        # route answers "did an assigned reviewer look", and an approval
        # that names another revision is not an answer to that either.
        ok, text = verdict(
            payload(
                [review("maxi-reviewer", "APPROVED", commit=OTHER_HEAD, is_bot=True)],
                roster=roster(),
            )
        )
        self.assertFalse(ok, text)
        self.assertIn("FAIL", text)

    def test_the_same_approval_at_the_current_head_does_satisfy_it(self):
        # The control for the case above: same reviewer, same state, same
        # payload shape, only the commit differs. Without this pair the
        # stale-head test could pass because the reviewer is not credited
        # for some unrelated reason.
        ok, text = verdict(
            payload(
                [review("maxi-reviewer", "APPROVED", commit=HEAD, is_bot=True)],
                roster=roster(),
            )
        )
        self.assertTrue(ok, text)

    def test_a_human_approval_at_head_satisfies_a_rostered_pull_request(self):
        # The roster only ever names bots, so before this path a human
        # approval could not satisfy a rostered pull request at all.
        ok, text = verdict(
            payload(
                [review("some-human", "APPROVED", is_bot=False)],
                roster=roster(),
            )
        )
        self.assertTrue(ok, text)
        self.assertIn("non-author account", text)

    def test_the_approval_path_needs_no_roster_at_all(self):
        # A roster status is published per head SHA and is absent in the
        # window before the selector runs. The approval path is not a
        # function of it.
        ok, text = verdict(
            payload(
                [review("codacy-production", "APPROVED", is_bot=True)],
            )
        )
        self.assertTrue(ok, text)


class TheAllowlistIsDecisive(unittest.TestCase):
    """A bot's approval counts only if the bot is ON the list."""

    def test_an_allowlisted_bot_is_refused_when_it_is_not_on_the_list(self):
        # `some-other-review-bot` reads diffs as well as any of them, and
        # the list is what says so. An allowlist that credited anything
        # that looked like a bot would not be an allowlist.
        ok, text = verdict(
            payload(
                [review("some-other-review-bot", "APPROVED", is_bot=True)],
                roster=roster(),
            )
        )
        self.assertFalse(ok, text)

    def test_an_unreadable_actor_type_is_not_read_as_a_human(self):
        # `isBot` is the collector's reading of GraphQL's `__typename`. When
        # it is absent the login's spelling decides -- `[bot]` is a bot, a
        # bare slug is UNKNOWN -- and unknown must not fall through to "a
        # human approved this". Otherwise every non-allowlisted automation
        # reaches the path the allowlist exists to gate.
        doc = payload(
            [{"author": "some-other-review-bot", "state": "APPROVED", "commit": HEAD}],
            roster=roster(),
        )
        ok, text = verdict(doc)
        self.assertFalse(ok, text)

    def test_a_bracketed_bot_login_is_still_a_bot_without_the_field(self):
        doc = payload(
            [
                {
                    "author": "some-other-review-bot[bot]",
                    "state": "APPROVED",
                    "commit": HEAD,
                }
            ],
            roster=roster(),
        )
        ok, text = verdict(doc)
        self.assertFalse(ok, text)

    def test_a_bracketed_allowlisted_login_counts_without_the_field(self):
        # A port of the collector to REST would spell the same actor
        # `maxi-reviewer[bot]`, and it must not read as a human approval
        # for a reason nobody can see.
        doc = payload(
            [{"author": "maxi-reviewer[bot]", "state": "APPROVED", "commit": HEAD}],
            roster=roster(),
        )
        ok, text = verdict(doc)
        self.assertTrue(ok, text)
        self.assertIn("trusted review bot", text)

    def test_every_roster_reviewer_is_on_the_allowlist(self):
        # The two lists answer different questions -- the roster maps a
        # LABEL to a login, the allowlist answers "is this reviewer
        # trusted" -- and a reviewer the roster can ask for that the
        # allowlist cannot credit is a reviewer whose approval would be
        # discarded. Stated as a test so adding a roster row without the
        # trust decision is a failure here rather than a silent hole.
        for label, login in gate.LABEL_TO_LOGIN.items():
            with self.subTest(label=label):
                self.assertTrue(
                    gate.is_trusted_review_bot(login),
                    label + " maps to " + login + ", which TRUSTED_REVIEW_BOTS"
                    " does not carry; a review from it could be asked for and"
                    " never credited",
                )

    def test_the_allowlist_is_defined_in_exactly_one_place(self):
        # "Keep the allowlist in one place" is only true while there is one
        # place. A second literal set in this repository -- in the collector,
        # in the workflow, in a docs example -- is the drift this constant
        # exists to prevent. Prose may NAME the constant (the workflow header
        # does, so a reader can find it); only the gate may define it.
        carriers = []
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            body = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"TRUSTED_REVIEW_BOTS\s*[:=]", body):
                carriers.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(
            carriers,
            [".github/actions/pr-review-gate/pr_review_gate.py"],
            "something other than the gate defines the trusted-review-bot allowlist",
        )


class TheRosterPathIsUnchanged(unittest.TestCase):
    """Condition 2's first route, and the measurement that keeps it."""

    def test_a_roster_asked_reviewer_still_satisfies_a_commented_review(self):
        # NOT a bug and not an oversight. Reviewers in this fleet submit
        # COMMENTED reviews and nothing else (measured 2026-09-28: 0
        # APPROVED reviews across 604 open PRs in 19 repositories), so this
        # route is the only one that passes today. Removing it would red
        # `review-gate/non-author-review` fleet-wide, which is not what a
        # decision about bots' approvals is for.
        ok, text = verdict(
            payload(
                [review("maxi-reviewer", "COMMENTED", is_bot=True)],
                roster=roster(),
            )
        )
        self.assertTrue(ok, text)
        self.assertIn("reviewed by 1 non-author reviewer(s)", text)
        self.assertIn("roster=present", text)

    def test_a_reviewer_the_roster_did_not_ask_for_still_does_not_satisfy_it(self):
        # The other half of the same rule: the roster narrows the set, and
        # `cubic` was skipped on this pull request.
        ok, text = verdict(
            payload(
                [review("cubic-dev-ai", "COMMENTED", is_bot=True)],
                roster=roster(),
            )
        )
        self.assertFalse(ok, text)
        self.assertIn("roster asked for", text)

    def test_an_approval_that_is_also_asked_for_names_both(self):
        # Both routes can hold at once. The verdict says who approved AND
        # keeps the roster line, so a reader sees the case it actually was
        # rather than whichever branch was checked first.
        ok, text = verdict(
            payload(
                [review("maxi-reviewer", "APPROVED", is_bot=True)],
                roster=roster(),
            )
        )
        self.assertTrue(ok, text)
        self.assertIn("maxi-reviewer approved commit", text)

    def test_the_threads_verdict_is_untouched_by_an_approval(self):
        doc = payload([review("maxi-reviewer", "APPROVED", is_bot=True)])
        doc["threads"] = [
            {
                "isResolved": False,
                "path": "a.rs",
                "url": "https://example.invalid/1",
                "author": "coderabbitai",
            }
        ]
        ok, lines = gate.evaluate(doc, only=gate.ONLY_THREADS)
        self.assertFalse(ok)
        self.assertIn("unresolved review thread", "\n".join(lines))


class TheAbsentRosterFallback(unittest.TestCase):
    """No roster status for this head: which accounts still count.

    The fallback used to credit ANY non-author review, and the accounts it
    actually credited included the org's own automation: measured 2026-09-28
    over 101 open pull requests in six repositories, ten relied on the
    fallback and three of them (`maxi-core#4792`, `maxi-ml#2660`,
    `maxi-dist#368`) were green on nothing but a review from
    `maxi-tools-auth[bot]`, the app identity that opens the pull request.
    "Somebody other than the author reviewed this change" is not answered by
    an account that never reviews changes.
    """

    def test_a_review_bot_still_satisfies_it(self):
        ok, text = verdict(
            payload(
                [review("maxi-reviewer", "COMMENTED", is_bot=True)],
            )
        )
        self.assertTrue(ok, text)

    def test_an_app_identity_does_not(self):
        ok, text = verdict(
            payload(
                [review("maxi-tools-auth[bot]", "COMMENTED", is_bot=True)],
            )
        )
        self.assertFalse(ok, text)
        self.assertIn("none is from a", text)
        self.assertIn("maxi-tools-auth[bot]", text)

    def test_github_actions_does_not(self):
        # The org's own policy for this bot, stated elsewhere in the fleet:
        # an empty `github-actions` review must not be taught to the gate as
        # a look. This is where that policy is enforced.
        ok, text = verdict(
            payload(
                [review("github-actions[bot]", "COMMENTED", is_bot=True)],
            )
        )
        self.assertFalse(ok, text)

    def test_a_human_approval_still_satisfies_it(self):
        ok, text = verdict(
            payload(
                [review("some-human", "APPROVED", is_bot=False)],
            )
        )
        self.assertTrue(ok, text)


class FailureSaysHowToFixIt(unittest.TestCase):
    def test_the_failure_names_the_approval_route(self):
        ok, text = verdict(payload([], roster=roster()))
        self.assertFalse(ok, text)
        self.assertIn("APPROVED review of this head SHA", text)

    def test_a_missing_head_sha_credits_no_approval(self):
        # No head to compare against is not a licence to assume the
        # approval was of the current revision. The condition stays
        # unsatisfied, which is the safe direction.
        doc = payload(
            [review("maxi-reviewer", "APPROVED", is_bot=True)], roster=roster()
        )
        del doc["headSha"]
        ok, text = verdict(doc)
        self.assertFalse(ok, text)

    def test_a_review_with_no_commit_credits_no_approval(self):
        doc = payload(
            [{"author": "maxi-reviewer", "state": "APPROVED", "isBot": True}],
            roster=roster(),
        )
        ok, text = verdict(doc)
        self.assertFalse(ok, text)


def _payload_program():
    """The jq program the collector actually runs to build payload.json."""
    text = COLLECTOR.read_text(encoding="utf-8")
    start = text.index("jq -n --slurpfile t threads.json --slurpfile r reviews.json")
    # The program is a single-quoted shell string, opened by the quote that
    # follows `--arg head_sha "$HEAD_SHA"`.
    open_quote = text.index("'", text.index('"$HEAD_SHA"', start))
    end = text.index("' > payload.json", start)
    return text[open_quote + 1 : end]


class TheCollectorCarriesTheEvidence(unittest.TestCase):
    """The approval path is only as good as the fields it reads."""

    def test_the_reviews_query_asks_for_the_actor_type_and_the_commit(self):
        query = COLLECTOR.read_text(encoding="utf-8")
        self.assertIn("author{login __typename}", query)
        self.assertIn("commit{oid}", query)

    def test_the_payload_program_emits_head_sha_commit_and_is_bot(self):
        if shutil.which("jq") is None:
            self.skipTest(
                "jq is not on PATH; the program is run, not read,"
                " and reading it is what this test refuses to do"
            )
        threads = [
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "author": {"login": AUTHOR},
                            "isDraft": False,
                            "headRefName": "wt/x",
                            "labels": {"nodes": []},
                            "reviewThreads": {"nodes": []},
                        }
                    }
                }
            }
        ]
        reviews = [
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviews": {
                                "nodes": [
                                    {
                                        "author": {
                                            "login": "maxi-reviewer",
                                            "__typename": "Bot",
                                        },
                                        "state": "APPROVED",
                                        "commit": {"oid": HEAD},
                                    },
                                    {
                                        "author": {
                                            "login": AUTHOR,
                                            "__typename": "User",
                                        },
                                        "state": "COMMENTED",
                                        "commit": {"oid": HEAD},
                                    },
                                    {
                                        "author": None,
                                        "state": "PENDING",
                                        "commit": None,
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        ]
        # The collector reads these with `--slurpfile`, which takes paths and
        # not stdin: two slurpfiles cannot share one stream.
        with tempfile.TemporaryDirectory() as tmp:
            threads_path = pathlib.Path(tmp) / "threads.json"
            reviews_path = pathlib.Path(tmp) / "reviews.json"
            threads_path.write_text(json.dumps(threads), encoding="utf-8")
            reviews_path.write_text(json.dumps(reviews), encoding="utf-8")
            proc = subprocess.run(
                [
                    "jq",
                    "-n",
                    "--slurpfile",
                    "t",
                    str(threads_path),
                    "--slurpfile",
                    "r",
                    str(reviews_path),
                    "--arg",
                    "head_sha",
                    HEAD,
                    _payload_program(),
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["headSha"], HEAD)
        self.assertEqual(
            doc["reviews"][0],
            {
                "author": "maxi-reviewer",
                "state": "APPROVED",
                "commit": HEAD,
                "isBot": True,
            },
        )
        # A human account reads as NOT a bot, which is what lets the
        # non-author-account route exist at all.
        self.assertIs(doc["reviews"][1]["isBot"], False)
        # And an unreadable actor type reads as null, not false: the gate
        # must not be handed "human" for an actor nobody could identify.
        self.assertIsNone(doc["reviews"][2]["isBot"])

    def test_the_collected_shape_is_enough_to_satisfy_the_gate(self):
        # End to end over the two halves: the collector's own output shape,
        # judged by the gate. A field renamed on one side and not the other
        # fails here rather than in production, where it would read as
        # "nobody reviewed".
        collected = {
            "author": AUTHOR,
            "isDraft": False,
            "headRefName": "wt/x",
            "labels": [],
            "headSha": HEAD,
            "threads": [],
            "reviews": [
                {
                    "author": "coderabbitai",
                    "state": "APPROVED",
                    "commit": HEAD,
                    "isBot": True,
                }
            ],
        }
        ok, lines = gate.evaluate(collected, only=gate.ONLY_REVIEWER)
        self.assertTrue(ok, "\n".join(lines))


class TheGateAsTheWorkflowRunsIt(unittest.TestCase):
    """Exit codes, through the CLI the composite action actually invokes.

    `evaluate()` returning False is not yet a red check: the workflow reads an
    exit code. One case per acceptance assertion, run the way the runner runs
    it, so a change that breaks the plumbing between the two cannot hide
    behind a green unit test.
    """

    def _run(self, doc):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "payload.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            return subprocess.run(
                ["python3", str(GATE), str(path), "--only", "non-author-review"],
                capture_output=True,
                text=True,
            )

    def test_exit_codes_for_the_four_decision_cases(self):
        cases = [
            (
                "an allowlisted bot approved this head",
                payload(
                    [review("cubic-dev-ai", "APPROVED", is_bot=True)], roster=roster()
                ),
                0,
            ),
            (
                "a bot commented, and the roster asked for someone else",
                payload(
                    [review("cubic-dev-ai", "COMMENTED", is_bot=True)], roster=roster()
                ),
                1,
            ),
            (
                "the author approved",
                payload([review(AUTHOR, "APPROVED", is_bot=False)]),
                1,
            ),
            (
                "an approval of a stale head",
                payload(
                    [
                        review(
                            "maxi-reviewer", "APPROVED", commit=OTHER_HEAD, is_bot=True
                        )
                    ],
                    roster=roster(),
                ),
                1,
            ),
        ]
        for what, doc, want in cases:
            with self.subTest(what=what):
                proc = self._run(doc)
                self.assertEqual(
                    proc.returncode, want, what + ": " + proc.stdout + proc.stderr
                )

    def test_a_failure_annotates_a_single_line(self):
        # `::error::` annotations render as PR annotations; a multi-line one
        # is dropped by the runner, which would leave a red check with no
        # visible reason.
        proc = self._run(payload([], roster=roster()))
        self.assertEqual(proc.returncode, 1)
        annotations = [l for l in proc.stdout.splitlines() if l.startswith("::error::")]
        self.assertTrue(annotations)
        for line in annotations:
            self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()
