#!/usr/bin/env python3
"""Decide whether a pull request has cleared the review bar.

Two conditions, both required:
  1. ZERO unresolved review threads.
  2. At least one review from somebody who is NOT the pull request author.

Either condition can be judged on its own via --only, so each can be reported
as its OWN check whose name states the action it wants. One check covering both
cannot: its name has to describe the passing state, and a reader who sees that
name go red has to negate a conjunction, which leaves them guessing which half
failed. Three successive renames failed to stop readers guessing "it wants a
human approval" -- the more salient of the two nouns -- so the name stopped
being the place to fix it. See the workflow that calls this.

Condition 2 is not the same as 「has reviews」. Every agent lane in this org
authenticates as the same account, so a PR can accumulate a pile of reviews
that are all self-review; counting reviews would be satisfied by the author
reviewing their own work. Counting DISTINCT NON-AUTHOR reviewers is not.

Condition 2 has one escape hatch a human can reach for: the
`review-infra-unavailable` label, for when no reviewer can be summoned at all.
It waives condition 2 and nothing else, and it names itself in the commit
status so the merge is recorded as waived rather than as reviewed. See
REVIEW_INFRA_LABEL for why it is honoured here and not by the reviewer.

Exit 0 = gate passes. Exit 1 = gate fails, OR the input cannot be trusted.

That second half matters as much as the first. A gate that passes when its
input is missing, empty or malformed reports healthy for the most broken PR
there is, which is worse than having no gate at all: it manufactures
confidence. Every validation below therefore fails closed.
"""

import json
import os
import sys

# A review in these states is not a review signal: PENDING has not been
# submitted, and DISMISSED has been explicitly retracted.
COUNTED_STATES = {'APPROVED', 'CHANGES_REQUESTED', 'COMMENTED'}

# Head-branch prefix the config fan-out opens its pull requests on. It names
# every branch it pushes 「maxi-config-sync/<manifest>」. Matched as a PREFIX and
# not by equality on purpose: the manifest name is the second segment, so a
# third manifest added later is covered without touching this file.
FANOUT_BRANCH_PREFIX = 'maxi-config-sync/'

# ...and the identity that opens them. The prefix alone is NOT sufficient: a
# branch name is attacker-chosen, so 「starts with maxi-config-sync/」 would let
# anyone who can push a branch here name their way out of the review gate. Both
# halves are required together.
#
# Spelled WITHOUT the 「[bot]」 suffix, which is not cosmetic. collect-pr-review-
# state reads `author{login}` over GraphQL, and GraphQL returns a Bot actor's
# bare slug -- verified against a live fan-out PR:
#     author: { login: 「maxi-tools-auth」, __typename: 「Bot」 }
# The REST API spells the same actor 「maxi-tools-auth[bot]」, so both are
# accepted: if the collection step is ever ported to REST the bypass keeps
# working. Accepting the bracketed form costs nothing, because 「[」 and 「]」 are
# not legal in a GitHub login and no real account can ever claim it.
FANOUT_AUTHORS = frozenset({'maxi-tools-auth', 'maxi-tools-auth[bot]'})

# Dependabot, whose pull requests have no reviewer to summon.
#
# `secrets.APP_ID` / `APP_PRIVATE_KEY` are ordinary Actions secrets, and a
# Dependabot-triggered run gets the DEPENDABOT secrets scope instead. The org
# holds none, so those arrive as empty strings and every app-token step in the
# review lanes dies on 「The 'client-id' ... input must be set to a non-empty
# string」 -- maxi-review's `lint-gate` and `review` among them. Those two ARE
# the non-author reviewers a bot-authored PR ever gets. So condition 2 is not
# merely unmet on a Dependabot PR, it is unsatisfiable: no push can fix it,
# because the next push is Dependabot's too.
#
# This is the same shape as the routers' `dependabot-fallback`, and for the
# reason stated there: without it, a stricter gate turns 「dependency PRs merge
# with no review signal」 into 「dependency PRs can never merge」.
#
# BOTH halves again, and the same GraphQL/REST split as FANOUT_AUTHORS -- the
# collection step reads GraphQL, which gives a Bot actor's bare slug. Verified
# on a live Dependabot PR:
#     GraphQL author.login 「dependabot」   REST user.login 「dependabot[bot]」
# Both accepted so a port of the collection step to REST cannot silently strand
# every dependency PR; the bracketed form is unspoofable either way.
DEPENDABOT_BRANCH_PREFIX = 'dependabot/'
DEPENDABOT_AUTHORS = frozenset({'dependabot', 'dependabot[bot]'})


# The escape hatch for "the review infrastructure is genuinely unavailable".
#
# It is honoured HERE, on condition 2, and deliberately nowhere else. Its
# predecessor was honoured by the REVIEWER instead, through the review bot's
# `bypass_label` default, and never by this gate. That inverted its own
# purpose: the review bot is one of the non-author reviewers this condition
# accepts, so the label removed a reviewer from the pool while leaving intact
# the requirement that the pool be non-empty. A PR carrying it was strictly
# LESS mergeable, in exactly the situation it was reached for.
#
# So the capability moved to the half that is load-bearing. The review
# workflow now passes `bypass_label: ""`, the reviewer never self-disables, and this label
# says what a human actually needs to say: "no reviewer can be summoned, stop
# asking for one."
#
# Condition 2 ONLY, for the same reason as the Dependabot waiver below it:
# reviewer availability has nothing to do with whether the threads a reviewer
# already opened are resolved. Waiving both would discard a signal that is
# still perfectly measurable.
#
# This one is an ASSERTION, not a structural fact about the PR -- unlike draft,
# fan-out and Dependabot, which the gate can see for itself. That is why it is
# the only waiver that names itself in the commit-status description: a PR that
# merged under it must not be recorded as "reviewed by a non-author", which is
# a claim about work that did not happen.
REVIEW_INFRA_LABEL = 'review-infra-unavailable'

#: Prefix marking a line where a condition passed on a human's ASSERTION rather
#: than on anything the gate could see for itself. `waiver_label` parses it and
#: main() publishes it as a step output, so review-gate-reusable.yml can put the
#: label in the commit-status description instead of the passing state's
#: wording. A status reading "reviewed by a non-author" on a PR nobody reviewed
#: is the same class of defect as the silent skip this waiver replaces: a green
#: tick that means something other than what it says.
#:
#: Not applied to the draft, fan-out or Dependabot waivers. Those are structural
#: facts about the PR that a reader can re-derive from the PR itself, and
#: marking them would change the status description on every gate run in every
#: consumer to fix a problem they do not have.
WAIVED_PREFIX = 'WAIVED:'


class Malformed(Exception):
    pass


def _require(cond, msg):
    if not cond:
        raise Malformed(msg)


def load(path):
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            raw = fh.read()
    except OSError as exc:
        raise Malformed('could not read ' + path + ': ' + str(exc))
    _require(raw.strip(), 'payload is empty (the collection step produced nothing)')
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise Malformed('payload is not valid JSON: ' + str(exc))


#: The condition selectors --only accepts. THREADS and REVIEWER each judge one
#: half; ALL judges both and is what a caller gets by default.
ONLY_THREADS = 'threads'
ONLY_REVIEWER = 'non-author-review'
ONLY_ALL = 'all'
ONLY_CHOICES = (ONLY_ALL, ONLY_THREADS, ONLY_REVIEWER)


def evaluate(doc, only=ONLY_ALL):
    """Return (ok, lines). Raises Malformed on input that cannot be judged.

    `only` selects which condition to judge; the unselected one is not
    evaluated and contributes neither a verdict nor a line. Validation of the
    payload is NOT narrowed with it -- a malformed thread list still fails the
    reviewer-only check, because a payload we cannot trust is not a payload we
    can draw half a conclusion from.
    """
    _require(only in ONLY_CHOICES, 'unknown condition selector: ' + str(only))
    _require(isinstance(doc, dict), 'payload is not a JSON object')

    author = doc.get('author')
    _require(isinstance(author, str) and author.strip(),
             'payload has no PR author; cannot tell self-review from review')

    # Absence, a non-list, or a list of non-strings all mean "no waiver", not
    # "malformed". This field can only ever LOOSEN a verdict, so an unreadable
    # one has to land on enforcement -- the same direction headRefName fails in.
    # Requiring it would instead turn any upstream shape change into a red gate
    # on every PR in the fleet, which is the opposite of failing closed.
    raw_labels = doc.get('labels')
    labels = raw_labels if isinstance(raw_labels, list) else []

    reviews = doc.get('reviews')
    threads = doc.get('threads')
    _require(isinstance(reviews, list), 'payload field "reviews" is not a list')
    _require(isinstance(threads, list), 'payload field "threads" is not a list')

    lines = []

    if doc.get('isDraft') is True:
        return True, ['draft pull request - review gate not enforced']

    # The fan-out's own payload was reviewed in maxi-config, on the PR that
    # changed it. What lands here is a byte-for-byte copy of that payload in
    # each consumer repository, opened by an app identity that never authors anything
    # else -- so the reviewers those PRs used to summon were re-reviewing one
    # already-reviewed diff thirty-one times. Suppressing them (CodeRabbit's
    # ignore_usernames/ignore_title_keywords, and maxi-review's own head_ref
    # guard) removes the only non-author reviewers a fan-out PR ever had, which
    # would leave condition 2 unsatisfiable and this gate red forever.
    #
    # So the bypass is not a convenience: it is the half of that change that
    # keeps the gate honest. It has to land BEFORE the suppression, or the
    # fan-out strands. Like the draft case it short-circuits BOTH conditions --
    # there is no reviewer to open a thread once the reviewers are suppressed,
    # so judging threads alone would assert something no longer measurable.
    #
    # Gated on the AUTHOR as well as the branch. The branch prefix says which
    # PRs the fan-out opens; it does not say who opened them, and a head ref is
    # attacker-chosen. Prefix alone would have turned this bypass into a way for
    # anyone able to push a branch -- in a repo whose gate exists precisely to
    # stop unreviewed merges -- to name themselves past it.
    head_ref = doc.get('headRefName')
    if (isinstance(head_ref, str)
            and head_ref.startswith(FANOUT_BRANCH_PREFIX)
            and author in FANOUT_AUTHORS):
        return True, ['maxi-config fan-out branch - reviewed at source in maxi-config']

    # Dependabot: condition 2 ONLY, and deliberately not the whole gate.
    #
    # The fan-out bypass above short-circuits BOTH conditions because its
    # reviewers are suppressed outright, so there is nobody left to open a
    # thread and judging threads would assert something unmeasurable. That
    # reasoning does NOT carry here. When a human pushes to a dependabot/*
    # branch `github.actor` becomes that human, ordinary secrets ARE available,
    # and the review lanes run normally -- so threads on a Dependabot PR are
    # real, measurable, and must still be enforced. Bypassing both would have
    # thrown that away for nothing.
    #
    # Note this keys on the PR AUTHOR, which stays Dependabot across such a
    # push, while the workflow-side guards key on `github.actor`, which does
    # not. That asymmetry is correct: the workflows are asking 「can this run
    # read the secrets?」 and the gate is asking 「can this PR ever attract a
    # reviewer?」. They are different questions about the same PR.
    dependabot = (isinstance(head_ref, str)
                  and head_ref.startswith(DEPENDABOT_BRANCH_PREFIX)
                  and author in DEPENDABOT_AUTHORS)

    # Exact match on the name, never a prefix or a substring: a label is
    # repository-scoped and can only be applied by someone with write access,
    # but 「contains」 would let `review-infra-unavailable-followup` waive the
    # gate by accident.
    infra_waiver = any(lbl == REVIEW_INFRA_LABEL
                       for lbl in labels if isinstance(lbl, str))

    # --- condition 1: unresolved threads ---------------------------------
    unresolved = []
    for i, th in enumerate(threads):
        _require(isinstance(th, dict), 'thread ' + str(i) + ' is not an object')
        resolved = th.get('isResolved')
        # Absence is NOT 「resolved」. A shape change upstream must fail here
        # rather than quietly reclassify every thread as fine.
        _require(isinstance(resolved, bool),
                 'thread ' + str(i) + ' has no boolean isResolved field')
        if not resolved:
            unresolved.append(th)

    # --- condition 2: a non-author reviewer -------------------------------
    reviewers = []
    for i, rv in enumerate(reviews):
        _require(isinstance(rv, dict), 'review ' + str(i) + ' is not an object')
        state = rv.get('state')
        _require(isinstance(state, str), 'review ' + str(i) + ' has no state')
        if state.upper() not in COUNTED_STATES:
            continue
        who = rv.get('author')
        # A review whose author we cannot identify cannot be credited as a
        # non-author review; that is the exact hole this gate exists to close.
        if not isinstance(who, str) or not who.strip():
            continue
        if who == author:
            continue
        if who not in reviewers:
            reviewers.append(who)

    ok = True

    if only in (ONLY_ALL, ONLY_THREADS) and unresolved:
        ok = False
        lines.append('FAIL: ' + str(len(unresolved)) + ' unresolved review thread(s):')
        for th in unresolved:
            where = th.get('path') or '(no file)'
            url = th.get('url') or '(no url)'
            who = th.get('author') or 'unknown'
            flag = ' [outdated]' if th.get('isOutdated') is True else ''
            lines.append('  - ' + who + ' on ' + where + flag + ' -> ' + url)
        # The trap: resolving the last thread emits NO Actions event, so this
        # check stays red on a PR that is now clean. Say so here rather than
        # leaving it in a workflow comment nobody reads from a failed check.
        lines.append('  Resolving a thread fires no Actions event, so this check')
        lines.append('  will stay red until you re-run it or push. That is expected.')
    elif only in (ONLY_ALL, ONLY_THREADS):
        lines.append('ok: no unresolved review threads (' + str(len(threads)) + ' total)')

    if (only in (ONLY_ALL, ONLY_REVIEWER)
            and not reviewers and not dependabot and not infra_waiver):
        ok = False
        self_reviews = sum(
            1 for rv in reviews
            if isinstance(rv, dict) and rv.get('author') == author
            and isinstance(rv.get('state'), str)
            and rv['state'].upper() in COUNTED_STATES
        )
        lines.append('FAIL: no review from anyone other than the author (' + author + ').')
        if self_reviews:
            lines.append('  ' + str(self_reviews) + ' review(s) found, but all are by the author.')
            lines.append('  Self-review is not review. Every agent lane in this org')
            lines.append('  authenticates as the same account, so this is the common case.')
        else:
            lines.append('  No reviews at all. This does NOT need a human approval:')
            lines.append('  a COMMENTED review from any review bot satisfies it. Pushing')
            lines.append('  a commit is usually enough to summon them.')
    elif only in (ONLY_ALL, ONLY_REVIEWER) and not reviewers and dependabot:
        # Dependabot is checked before the label so a Dependabot PR that also
        # carries the label is still reported by its structural reason, which
        # is the true one and needs no human to have asserted anything.
        lines.append('ok: dependabot pull request - the review lanes cannot run '
                     'without Dependabot secrets, so no reviewer can be summoned')
    elif only in (ONLY_ALL, ONLY_REVIEWER) and not reviewers:
        # Reached only under the infrastructure waiver -- the branches above own
        # every other reviewer-less case.
        lines.append(WAIVED_PREFIX + ' ' + REVIEW_INFRA_LABEL
                     + ' - no non-author review; merging on the assertion that '
                       'no reviewer could be summoned')
    elif only in (ONLY_ALL, ONLY_REVIEWER):
        lines.append('ok: reviewed by ' + str(len(reviewers)) + ' non-author reviewer(s): '
                     + ', '.join(sorted(reviewers)))

    return ok, lines


def waiver_label(lines):
    """The label a condition was waived by, or '' when none was.

    Reads the evaluator's own output rather than re-deriving the decision from
    the payload, for the same reason failure_annotations does: a second place
    that computes the verdict is a second place that can drift from it.
    """
    for line in lines:
        if line.startswith(WAIVED_PREFIX):
            return line[len(WAIVED_PREFIX):].strip().split(' ', 1)[0]
    return ''


def emit_waiver(label):
    """Publish the waiver as a GitHub Actions step output, if we are in one.

    Allow-listed against the labels this gate actually honours before it is
    written. The value is our own constant today and cannot contain a newline,
    but $GITHUB_OUTPUT is a file format where a newline in a value sets
    ARBITRARY further outputs -- so the safety is asserted here rather than
    inherited from where the string happens to come from right now.

    A missing $GITHUB_OUTPUT is not an error: the script is run directly by
    tests/test_pr_review_gate.py, and refusing to judge a PR because nobody
    wanted the output would be a gate that fails on its own test suite.
    """
    if label not in ('', REVIEW_INFRA_LABEL):
        print('::warning::refusing to publish unrecognised waiver ' + repr(label))
        return
    path = os.environ.get('GITHUB_OUTPUT')
    if not path:
        return
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write('waiver=' + label + '\n')
    except OSError as exc:
        print('::warning::could not publish the waiver output: ' + str(exc))


def failure_annotations(lines):
    """The failing halves, as single-line GitHub `::error::` annotation texts.

    The gate tests two independent conditions, and a check name cannot say
    which of them failed. An annotation can: GitHub surfaces `::error::` on the
    PR's checks page, so the cause is visible without opening the log. A
    generic "see the lines above" put the cause one click away, and the
    conclusion people reached from the name alone -- that the gate wants a
    human approval -- was frequently the wrong half.

    Each `FAIL:` line the evaluator emits is already a complete sentence naming
    its own condition, so this reuses them rather than restating the verdict in
    a second place that could drift from it.
    """
    prefix = 'FAIL:'
    return [
        line[len(prefix):].strip().rstrip(':')
        for line in lines
        if line.startswith(prefix)
    ]


def parse_args(argv):
    """Return (payload_path, only). Raises Malformed on unusable arguments.

    Deliberately hand-rolled rather than argparse: an unknown --only value must
    fail closed with the same "cannot judge this PR" treatment as a malformed
    payload. argparse would exit 2 straight out of the parser, and a check that
    dies before it judges anything reads on the PR page as the very ambiguity
    this split exists to remove.
    """
    args = list(argv[1:])
    only = ONLY_ALL
    positional = []
    while args:
        arg = args.pop(0)
        if arg == '--only':
            _require(args, '--only needs a value ' + str(list(ONLY_CHOICES)))
            only = args.pop(0)
        elif arg.startswith('--only='):
            only = arg.split('=', 1)[1]
        else:
            positional.append(arg)
    _require(only in ONLY_CHOICES,
             'unknown --only value ' + repr(only) + '; expected one of '
             + ', '.join(ONLY_CHOICES))
    _require(len(positional) == 1,
             'expected exactly one payload path, got ' + str(len(positional)))
    return positional[0], only


def main(argv):
    try:
        payload_path, only = parse_args(argv)
        doc = load(payload_path)
        ok, lines = evaluate(doc, only)
    except Malformed as exc:
        print('::error::review gate could not evaluate this PR: ' + str(exc))
        print('Failing closed: an unjudgeable payload is not a pass.', file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    emit_waiver(waiver_label(lines))
    if not ok:
        annotations = failure_annotations(lines)
        if annotations:
            for annotation in annotations:
                print('::error::review gate: ' + annotation)
        else:
            # Unreachable while every failure path emits a FAIL: line, but a
            # silent red check would be worse than a generic one.
            print('::error::review gate failed - see the lines above')
        return 1
    print('review gate: passed')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
