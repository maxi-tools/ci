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
import re
import sys

# A review in these states is not a review signal: PENDING has not been
# submitted, and DISMISSED has been explicitly retracted.
COUNTED_STATES = {'APPROVED', 'CHANGES_REQUESTED', 'COMMENTED'}

# Head-branch prefix the maxi-config fan-out opens its pull requests on --
# $sync_branch_prefix in maxi-config's scripts/sync-maxi-review.sh, which names
# every branch it pushes 「maxi-config-sync/<manifest>」. Matched as a PREFIX and
# not by equality on purpose: the manifest name is the second segment, so a
# third manifest added later is covered without touching this file.
#
# RETIREMENT SCHEDULE (T4, maxi-config#735): this bypass is the only thing
# stopping the fan-out from waiting forever for a reviewer that the roster
# selector (T3, maxi-config#728, merged 2026-09-19) will deliberately skip.
# It stays in place until T3 has been live on every repo for 14 days -- a
# date set to 2026-10-03 -- so a PR whose head SHA lacks a `review-roster`
# status still passes the gate on a fan-out branch. After 2026-10-03 the
# selector is the single source of truth and this bypass can be removed
# alongside the FANOUT_AUTHORS half of the security guard below. The fan-out
# branch's `asked` list collapses to `[maxi-lint]` per the selector's
# fast-path, and the rest of the reviewers are listed in `skipped` with
# their canon reason -- the gate consumes that list directly once the
# roster is required.
FANOUT_BRANCH_PREFIX = 'maxi-config-sync/'

# The SECOND producer writing to the same consumers: maxi-tools/ci's
# fanout-ci-pin.yml, which advances each consumer's pinned `uses:` sha on
# `ci/fanout-pin`. Same App, different branch, and it was not enrolled --
# so on 2026-09-20 its 49 open PRs were structurally unmergeable: every
# review lane suppresses this author, and this bypass demanded a prefix
# those branches do not carry. (maxi-config#784.)
#
# Mirrors ci/fanout-identity.toml, INCLUDING its two shapes: a namespace
# ending in `/` is a prefix, a whole branch name is matched by equality.
# `ci/fanout-pin` is one branch per consumer (ci#32), so prefix-matching
# it would also accept `ci/fanout-pinned`. Lists rather than scalars
# because the next producer must extend one, not add a third test
# nobody can find. (codacy, #789.)
FANOUT_BRANCH_PREFIXES = (FANOUT_BRANCH_PREFIX,)
FANOUT_BRANCH_NAMES = ('ci/fanout-pin',)


def is_fanout_branch(head_ref):
    '''Does this head ref belong to a declared fan-out producer?

    The AUTHOR half is checked separately and is mandatory; see below.
    '''
    return (isinstance(head_ref, str)
            and (head_ref.startswith(FANOUT_BRANCH_PREFIXES)
                 or head_ref in FANOUT_BRANCH_NAMES))

# ...and the identity that opens them. The prefix alone is NOT sufficient: a
# branch name is attacker-chosen, so 「starts with maxi-config-sync/」 would let
# anyone who can push a branch here name their way out of the review gate. Both
# halves are required together.
#
# Spelled WITHOUT the 「[bot]」 suffix, which is not cosmetic. collect-pr-review-
# state reads `author{login}` over GraphQL, and GraphQL returns a Bot actor's
# bare slug -- verified against a live fan-out PR (maxi-kvm#24):
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
# This is the same shape as maxi-core ci.yml's `dependabot-fallback`, and for
# the reason stated there: without it, a stricter gate turns 「dependency PRs
# merge with no review signal」 into 「dependency PRs can never merge」.
#
# BOTH halves again, and the same GraphQL/REST split as FANOUT_AUTHORS -- the
# collection step reads GraphQL, which gives a Bot actor's bare slug. Verified
# on a live Dependabot PR (maxi-kvm#4):
#     GraphQL author.login 「dependabot」   REST user.login 「dependabot[bot]」
# Both accepted so a port of the collection step to REST cannot silently strand
# every dependency PR; the bracketed form is unspoofable either way.
DEPENDABOT_BRANCH_PREFIX = 'dependabot/'
DEPENDABOT_AUTHORS = frozenset({'dependabot', 'dependabot[bot]'})


# The escape hatch for "the review infrastructure is genuinely unavailable".
#
# It is honoured HERE, on condition 2, and deliberately nowhere else. Its
# predecessor -- `maxi-review-override` -- was honoured by the REVIEWER instead,
# through maxi-reviewer's `bypass_label` default, and never by this gate. That
# inverted its own purpose: `maxi-reviewer[bot]` is one of the non-author
# reviewers this condition accepts, so the label removed a reviewer from the
# pool while leaving intact the requirement that the pool be non-empty. A PR
# carrying it was strictly LESS mergeable, in exactly the situation it was
# reached for. Measured on maxi-config#536; see issue #555.
#
# So the capability moved to the half that is load-bearing. maxi-review.yml now
# passes `bypass_label: ""`, the reviewer never self-disables, and this label
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
#: marking them would change the status description on every gate run in ~31
#: repos to fix a problem they do not have.
WAIVED_PREFIX = 'WAIVED:'


#: The roster the maxi-review selector publishes for every pull request, as a
#: commit status with context `review-roster`. The selector is the
#: generalisation of the fan-out bypass da0941c landed: instead of one
#: head_ref/author pair hard-coded into this gate, the selector picks the
#: reviewer set per PR and publishes the decision. The gate consumes the
#: roster's `asked` set as its required non-author reviewers -- a roster that
#: skips CodeRabbit on a trivial PR no longer leaves this gate waiting forever
#: for a review that was never requested.
#:
#: Status description format (single line, from
#: maxi-review/select-roster.py:summary):
#:
#:     asked=[a,b,...] skipped=<n> [unknown=<n>]
#:
#: The gate consumes the `asked` NAMES and the `skipped` COUNT. A review
#: counts only when its author is in `asked`; `skipped` is printed as a
#: length on the passing line and never matched against an author. `band`
#: and `profiles` are not on this line: GitHub caps the description at 140
#: characters, and the step summary prints both from the JSON. An operator
#: who needs to know WHY a reviewer was skipped follows the status's
#: target_url into the run.
#:
#: A head not re-reviewed since the shortening still carries the previous
#: wire format, which this parser also accepts:
#:
#:     band=<band> asked=[a,b,...] skipped=[c,d,...] profiles=<state>
#:
#: `skipped` there is a name list; the gate only ever reads the length, so
#: the parser normalises it to a count. Dropping the line would fail the
#: gate closed on every such head.
ROSTER_CONTEXT = 'review-roster'

#: Roster description regex. Tolerates whitespace between the fields and an
#: optional trailing newline the collector may append after the description.
#: The selector emits the fields on one line with single spaces between them,
#: but a hand-edited description with a final `\n` would otherwise fail to
#: match. The selector never emits whitespace around `=`; matching `\s+`
#: between the fields is enough to survive a copy-paste through a markdown
#: renderer, which is how a human-readable status gets re-pasted into the
#: runner.
#:
#: Asked names are bracket-delimited and comma-separated, may be empty, and
#: are GitHub reviewer slugs (login or login[bot], both accepted as the
#: GraphQL/REST split elsewhere in this file handles). `skipped` is a count:
#: one or more digits. A description that still carries the old
#: comma-separated name list parses too -- the count is the number of names
#: -- so a status published before this change does not fail the gate closed.
#:
#: Two shapes match. The current one starts at `asked=`. The previous one
#: starts at `band=` and ends at `profiles=`; its `skipped` group is the
#: name list, which parse_roster counts. A description that starts with
#: either prefix and matches neither is a corruption and fails closed.
ROSTER_DESCRIPTION_RE = re.compile(
    r'^(?:band=[a-z]+\s+)?'
    r'asked=\[([^\]]*)\]\s+'
    r'skipped=([^\s]+)'
    r'(?:\s+unknown=(\d+))?'
    r'(?:\s+profiles=\S+)?'
    r'\s*$'
)


def _skipped_count(roster):
    """The number of skipped reviewers, whether the payload carries a count or a list.

    The selector publishes a count, because the description is capped at 140
    characters and the gate only ever prints the length. A payload built
    before that change, or a test, still carries the name list. Both answer
    the same question.
    """
    skipped = roster.get('skipped')
    if isinstance(skipped, int):
        return skipped
    if isinstance(skipped, list):
        return len(skipped)
    return 0


def parse_roster(description):
    """Parse a `review-roster` status description into asked names and a skip count.

    Returns None when the description cannot be trusted to be a roster --
    a None roster tells the gate to behave exactly as it did before T3,
    failing closed to the old "any non-author review" rule.

    Raises Malformed when the description LOOKS like a roster (the prefix
    matches) but the body is unparseable. That is the dangerous shape: a
    selector that is publishing the wrong thing in the right slot, which an
    `ignored` rule would silently turn into a green gate.

    `asked` is a list of names. `skipped` is a count: the selector publishes
    a number because GitHub caps the description at 140 characters and the
    gate only ever reads the length. A description that still carries the
    old comma-separated name list parses as well, and the count is the
    number of names, so a status published before the change does not fail
    closed. That includes the complete previous wire format, which starts
    with `band=` and ends with `profiles=` and is still published on every
    head not re-reviewed since the shortening.
    """
    if not isinstance(description, str) or not description.strip():
        return None
    match = ROSTER_DESCRIPTION_RE.match(description.strip())
    if not match:
        # Decide between "this is not a roster description" and "this is a
        # corrupted roster description". The selector publishes either the
        # current `asked=` line or the previous `band=` line, so a
        # description that starts with neither is an unrelated context or
        # human editing -- "not a roster". A description that starts with
        # one of those prefixes and then fails to match is a corruption
        # and must fail closed.
        stripped = description.lstrip()
        if stripped.startswith('asked=') or stripped.startswith('band='):
            raise Malformed(
                'review-roster description is not in the expected shape: '
                + repr(description)
            )
        return None

    def _split(raw):
        return [name for name in raw.split(',') if name]

    skipped_raw = match.group(2).strip('[]')
    if skipped_raw.isdigit():
        skipped = int(skipped_raw)
    else:
        skipped = len(_split(skipped_raw))

    return {'asked': _split(match.group(1)),
            'skipped': skipped}


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

    # Roster is OPTIONAL. Absent field, None, or a non-object all mean "no
    # roster was published for this head SHA" -- the gate falls back to the
    # pre-roster rule and says `roster=absent` in its output. A present roster
    # whose description cannot be parsed fails the whole evaluation closed
    # (see parse_roster); the field's job here is to widen the verdict to
    # one the gate cannot reach from the reviews alone.
    raw_roster = doc.get('roster')
    if raw_roster is None:
        roster = None
    else:
        _require(isinstance(raw_roster, dict),
                 'payload field "roster" is not an object')
        _require('asked' in raw_roster and isinstance(raw_roster['asked'], list)
                 and all(isinstance(n, str) for n in raw_roster['asked']),
                 'payload field "roster.asked" is not a list of strings')
        _require('skipped' in raw_roster and (
                     (isinstance(raw_roster['skipped'], int) and raw_roster['skipped'] >= 0)
                     or (isinstance(raw_roster['skipped'], list)
                         and all(isinstance(n, str) for n in raw_roster['skipped']))),
                 'payload field "roster.skipped" is not a count or a list of strings')
        # A roster that names no one as `asked` cannot impose a narrower rule
        # than the old "any non-author review" rule -- an empty `asked` is
        # indistinguishable from "the selector wanted no reviews at all", and
        # treating it as a stricter rule would mean a missing review fails the
        # gate even though the selector did not ask for one. Fall back to the
        # pre-roster rule in this single case, and surface the empty list so a
        # reader sees it.
        roster = raw_roster

    lines = []

    if doc.get('isDraft') is True:
        return True, ['draft pull request - review gate not enforced']

    # The fan-out's own payload was reviewed in maxi-config, on the PR that
    # changed it. What lands here is a byte-for-byte copy of that payload in
    # each of ~31 repos, opened by an app identity that never authors anything
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
    # stop unreviewed merges -- to name themselves past it. (maxi-reviewer, #468.)
    head_ref = doc.get('headRefName')
    if is_fanout_branch(head_ref) and author in FANOUT_AUTHORS:
        return True, ['fan-out branch - reviewed at source (maxi-config or ci)']

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

    # Roster narrows condition 2 to the selectors asked set, when one is
    # present AND non-empty. An absent roster or an empty asked list both fall
    # back to the pre-roster rule: any non-author review suffices. A `skipped`
    # reviewer that happens to run (e.g. a dashboard-only bot whose lane is
    # not gated by the roster) STILL appears in `reviewers` -- the listing is
    # who actually reviewed, not the gate's required set -- but it does NOT
    # end up in `covered`, because the asked set is what the gate waits for.
    # That is the difference between "this bot was deliberately skipped" and
    # "this bot silently failed": the roster says the first, the absence of
    # a status says the second, and the gate honours whichever signal it sees.
    # `active_roster` is the roster dict that applies, or None. Computed
    # once and read by every branch below, so a typo in one branch does
    # not silently desync from the others.
    if roster is not None and roster.get('asked'):
        active_roster = roster
        # Match the bracket-tolerant GraphQL/REST split the FANOUT_AUTHORS
        # block above already handles: a roster entry is a bare slug, and
        # REST-port code may spell the same actor with `[bot]`. The
        # intersection accepts both, just like the existing author checks.
        asked_logins = {name.removesuffix('[bot]')
                        for name in active_roster['asked']}
        covered = [name for name in reviewers
                   if name.removesuffix('[bot]') in asked_logins]
    else:
        active_roster = None
        asked_logins = None
        covered = list(reviewers)

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
            and not covered and not dependabot and not infra_waiver):
        ok = False
        self_reviews = sum(
            1 for rv in reviews
            if isinstance(rv, dict) and rv.get('author') == author
            and isinstance(rv.get('state'), str)
            and rv['state'].upper() in COUNTED_STATES
        )
        if asked_logins is not None:
            # Roster present and non-empty: the FAIL has to name the asked
            # set so an operator knows which reviewer to summon. The gate is
            # here because nobody on the asked list has reviewed -- a
            # `skipped` reviewer that ran anyway would NOT be in `covered`
            # (covered is reviewers ∩ asked), but its presence in
            # `reviewers` did not satisfy the required set. Surface that
            # distinction so a triage reader does not chase a reviewer the
            # roster said to skip.
            lines.append('FAIL: roster asked for ' +
                         ', '.join(sorted(active_roster['asked'])) +
                         ' but none of them has reviewed.')
            lines.append('  No human approval is wanted; a COMMENTED review from any')
            lines.append('  reviewer on the asked list satisfies this. Pushing a')
            lines.append('  commit is usually enough to summon them.')
        else:
            lines.append('FAIL: no review from anyone other than the author (' + author + ').')
            if self_reviews:
                lines.append('  ' + str(self_reviews) + ' review(s) found, but all are by the author.')
                lines.append('  Self-review is not review. Every agent lane in this org')
                lines.append('  authenticates as the same account, so this is the common case.')
            else:
                lines.append('  No reviews at all. This does NOT need a human approval:')
                lines.append('  a COMMENTED review from any review bot satisfies it. Pushing')
                lines.append('  a commit is usually enough to summon them.')
    elif only in (ONLY_ALL, ONLY_REVIEWER) and not covered and dependabot:
        # Dependabot is checked before the label so a Dependabot PR that also
        # carries the label is still reported by its structural reason, which
        # is the true one and needs no human to have asserted anything.
        lines.append('ok: dependabot pull request - the review lanes cannot run '
                     'without Dependabot secrets, so no reviewer can be summoned')
    elif only in (ONLY_ALL, ONLY_REVIEWER) and not covered:
        # Reached only under the infrastructure waiver -- the branches above own
        # every other reviewer-less case.
        lines.append(WAIVED_PREFIX + ' ' + REVIEW_INFRA_LABEL
                     + ' - no non-author review; merging on the assertion that '
                       'no reviewer could be summoned')
    elif only in (ONLY_ALL, ONLY_REVIEWER):
        # The roster-aware line names only the asked reviewers that DID
        # review, not the full `reviewers` set -- `skipped` reviewers are
        # deliberately not in the asked list, and reporting them here would
        # confuse a reader who reads "X reviewed" without the roster context
        # and assumes X was required. The roster context goes on the line
        # below.
        listed = sorted(covered) if asked_logins is not None else sorted(reviewers)
        lines.append('ok: reviewed by ' + str(len(listed)) + ' non-author reviewer(s): '
                     + ', '.join(listed))
        if asked_logins is not None:
            # Surface the roster shape on every passing line: a reader of the
            # log who can see CodeRabbit skipped it knows the gate was not
            # waiting for CodeRabbit. Naming `skipped=` here would also be
            # correct but adds noise on PRs where `skipped=[]`; keep the
            # one-line summary tight and put `profiles=` in the workflow's
            # own step summary instead. `asked=` already shows up via the
            # FAIL branch above; the PASS branch repeats the size so a
            # reader scanning the log sees how many reviewers the roster
            # asked for.
            lines.append('  roster=present asked=' + str(len(active_roster['asked']))
                         + ' skipped=' + str(_skipped_count(active_roster)))
        else:
            lines.append('  roster=absent - behaving as before the roster selector shipped')

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
