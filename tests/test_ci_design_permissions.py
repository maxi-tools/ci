#!/usr/bin/env python3
"""A documented caller permission map IS the floor the lanes impose.

`docs/ci-design.md`'s "Consuming these" section hands a caller a `permissions:`
map to copy. That map is not advice. GitHub validates the permissions of the
whole nested call graph when it CREATES the run, before any job `if:` is
evaluated, so:

  * a scope a lane declares is a scope EVERY caller has to grant, on every
    event -- including the events where that lane is skipped -- and
  * no caller-side change can lower it.

A documented map that is MISSING a scope breaks every caller who copies it
(`startup_failure`: zero jobs, no log). A documented map that carries an EXTRA
scope is the over-grant that `lane-sign-publish.yml`'s trim existed to remove:
it made every consumer of `rust-ci.yml` hand out `contents: write`,
`id-token: write` and `pages: write` on pull requests, for steps that touch
none of them.

Both directions are checked here -- against the call graph and this
repository's own wrappers, never against a second hand-written list that could
be edited into agreement with a wrong doc.

The floor is computed TRANSITIVELY, because a nested declaration is a floor
too: `rust-ci.yml`'s jobs call six lanes, each of which declares job-level
permissions of its own, and a consumer's grant is measured against all of
them. That is the whole reason the trim had to land in the lane as well as at
its call site.
"""

import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

RUST_CI = '.github/workflows/rust-ci.yml'
SIGN_PUBLISH_LANE = '.github/workflows/lane-sign-publish.yml'
REVIEW_GATE = '.github/workflows/review-gate-reusable.yml'
OWN_WRAPPER = '.github/workflows/review-gate.yml'
DESIGN = ROOT / 'docs' / 'ci-design.md'

#: `uses:` values that name a WORKFLOW IN THIS TREE. Two shapes matter:
#:
#:     ./.github/workflows/x.yml                     a relative call (lane chain)
#:     maxi-tools/ci/.github/workflows/x.yml@<sha>   a pinned first-party call
#:
#: The second is how a consumer pins; inside this tree it names one of these
#: same files, so a pin that is acceptable for a consumer is checkable here.
#: A reference to anything else (an action, or another repository's workflow)
#: is not a workflow edge in this graph and is not resolved.
SELF_WORKFLOW_REF = re.compile(
    r'^(?:\./)?(?:maxi-tools/ci/)?(\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml)(?:@(.*))?$'
)

#: The scopes a caller of `rust-ci.yml` must grant, exactly. Named as a
#: constant rather than derived so the expectation is readable in one place --
#: and it is stated POSITIVELY (these three, nothing else) because a fourth
#: read scope is as much a regression to notice as a write scope.
RUST_CI_FLOOR = {'contents': 'read', 'pull-requests': 'read', 'actions': 'read'}

#: The scopes a caller of the review gate must grant, exactly. `pull-requests`
#: is WRITE because of the `dedupe` leg, and `statuses` because of the
#: publisher; both are job-level declarations inside the callee.
REVIEW_GATE_FLOOR = {
    'actions': 'read',
    'contents': 'read',
    'pull-requests': 'write',
    'statuses': 'write',
}


def load_workflow(rel):
    path = ROOT / rel
    if not path.is_file():
        raise AssertionError(rel + ' does not exist in this tree')
    document = yaml.safe_load(path.read_text(encoding='utf-8'))
    if not isinstance(document, dict) or not isinstance(document.get('jobs'), dict):
        raise AssertionError(rel + ' has no jobs: mapping; this walker measured nothing')
    return document


def _fold(total, scopes):
    """Union a scope map into `total`, with `write` winning over `read`."""
    for scope, level in (scopes or {}).items():
        if total.get(scope) != 'write':
            total[scope] = level


def permission_floor(rel, seen=None):
    """Every scope a CALLER of `rel` has to grant, transitively.

    Includes the callee's own workflow-level map when it declares one (an
    empty `permissions: {}` declares nothing and contributes nothing), every
    job-level map in it, and -- for each job that calls a workflow in this
    tree -- the whole floor of that workflow. Cycles are cut by `seen`; a
    reference to a workflow in this tree that does not resolve fails closed
    rather than being skipped, because a skipped edge is a floor measured too
    low, which is a green test on a broken call graph.
    """
    seen = set() if seen is None else seen
    if rel in seen:
        return {}
    seen.add(rel)

    document = load_workflow(rel)
    total = {}
    _fold(total, document.get('permissions') if isinstance(document.get('permissions'), dict) else None)

    for job_name, job in document['jobs'].items():
        if not isinstance(job, dict):
            raise AssertionError(rel + ': job ' + job_name + ' is not a mapping')
        _fold(total, job.get('permissions') if isinstance(job.get('permissions'), dict) else None)

        uses = job.get('uses') if isinstance(job.get('uses'), str) else None
        if uses is None:
            continue
        match = SELF_WORKFLOW_REF.match(uses.strip())
        if match is None:
            continue
        target = match.group(1)
        if not (ROOT / target).is_file():
            raise AssertionError(
                rel + ': job ' + job_name + ' calls ' + uses + ', which resolves '
                'to no file in this tree -- the floor below would be measured '
                'without it, so this fails closed instead'
            )
        _fold(total, permission_floor(target, seen))
    return total


def job_permissions(rel, job_name):
    document = load_workflow(rel)
    job = document['jobs'].get(job_name)
    if not isinstance(job, dict):
        raise AssertionError(rel + ' has no job named ' + job_name)
    scopes = job.get('permissions')
    if not isinstance(scopes, dict):
        raise AssertionError(rel + ':' + job_name + ' declares no permissions map')
    return scopes


def documented_callers():
    """(job name, callee path, granted map) for every caller example in the doc.

    Only fenced ```yaml blocks are read: the design doc states its calling
    shapes in YAML and its prose in prose, and a map parsed out of prose is a
    map nobody was ever told to copy.
    """
    examples = []
    for block in re.findall(r'```yaml\n(.*?)```', DESIGN.read_text(encoding='utf-8'), re.DOTALL):
        document = yaml.safe_load(block)
        if not isinstance(document, dict):
            raise AssertionError('a ```yaml block in ' + str(DESIGN) + ' is not a mapping')
        for job_name, job in (document.get('jobs') or {}).items():
            uses = job.get('uses') if isinstance(job, dict) and isinstance(job.get('uses'), str) else None
            if uses is None:
                continue
            match = SELF_WORKFLOW_REF.match(uses.strip())
            if match is None:
                raise AssertionError(
                    'the documented caller ' + job_name + ' uses ' + uses + ', which '
                    'is not a workflow in this tree; its floor cannot be computed '
                    'here, so this fails closed rather than leaving it unmeasured'
                )
            examples.append((job_name, match.group(1), job.get('permissions') or {}))
    return examples


class CallerPermissionFloor(unittest.TestCase):

    def test_rust_ci_call_graph_requires_only_read_scopes(self):
        """The regression this lane exists for: nothing in the graph writes.

        The declaration removed here was a FLOOR on every consumer, not a
        request confined to the runs where the lane applies, so a write scope
        reintroduced anywhere in the graph -- in a lane, or at a call site
        inside `rust-ci.yml` -- is immediately a write scope every consumer of
        `rust-ci.yml` must grant on every event, pull requests included.
        """
        floor = permission_floor(RUST_CI)
        self.assertEqual(
            floor, RUST_CI_FLOOR,
            'the transitive floor of ' + RUST_CI + ' changed. Every consumer of '
            'this workflow must grant exactly this map, on every event, before '
            'any job `if:` is evaluated -- so a scope added here is a scope '
            'added to ~48 repositories, and a write scope is the over-grant '
            'this change removed'
        )
        for scope, level in floor.items():
            self.assertEqual(
                level, 'read',
                'the graph declares ' + scope + ': ' + level + '; no step in it '
                'needs a write scope, and a declaration here is a consumer-side '
                'requirement (maxi-config#366)'
            )

    def test_sign_publish_declares_only_the_permissions_its_steps_use(self):
        """The lane AND its call site, as exact maps.

        Both layers matter and they are not the same claim: the lane's map is
        the floor for a direct caller of the lane, and the call site's map in
        `rust-ci.yml` is the floor every consumer of `rust-ci.yml` is measured
        against. A trim in one place and not the other is a half-fix that still
        forces the trio on every consumer.
        """
        self.assertEqual(
            job_permissions(SIGN_PUBLISH_LANE, 'lane-sign-publish'),
            {'contents': 'read'},
            'lane-sign-publish declares more than contents: read; its steps are a '
            'checkout with persist-credentials: false, an artifact download and '
            'two echoes'
        )
        self.assertEqual(
            job_permissions(RUST_CI, 'sign-publish'),
            {'contents': 'read'},
            'rust-ci.yml\'s sign-publish call site declares more than '
            'contents: read; this map, not the lane\'s, is what a consumer\'s '
            'caller grant is measured against'
        )

    def test_documented_caller_permissions_are_exactly_the_floor(self):
        """The doc's examples are the floor, no more and no less.

        An extra scope is the defect codex reported on #67 -- the example told
        new consumers to keep exposing write-capable tokens that this change
        makes unnecessary -- and a missing scope is worse: it is a documented
        caller that fails at run creation.
        """
        examples = documented_callers()
        by_callee = {callee: (job, granted) for job, callee, granted in examples}

        # Non-vacuity. A doc whose examples stopped parsing, or a heading that
        # took them with it, must fail rather than measure an empty set.
        self.assertEqual(
            sorted(by_callee), [REVIEW_GATE, RUST_CI],
            'the design doc no longer documents exactly these two calling shapes: '
            + repr(sorted(by_callee))
        )

        for callee, (job_name, granted) in sorted(by_callee.items()):
            floor = permission_floor(callee)
            self.assertTrue(floor, callee + ' has no permission floor at all; the walker measured nothing')
            self.assertEqual(
                granted, floor,
                'the documented permissions for ' + job_name + ' (' + callee + ') '
                'are not the floor of that callee. Missing scopes break every '
                'caller who copies the example at run creation; extra scopes are '
                'the over-grant this change removed. Grant what the graph uses.'
            )

    def test_this_repositorys_own_wrapper_grants_the_gate_floor(self):
        """`review-gate.yml` is a caller too, and the same rule applies.

        It is the call site that makes `review-gate/threads` and
        `review-gate/non-author-review` exist on this repository's own pull
        requests, so a grant that is one scope short would take the gate off the
        board here -- silently, at run creation -- which is the exact failure
        this test exists to make impossible.
        """
        self.assertEqual(
            permission_floor(OWN_WRAPPER), REVIEW_GATE_FLOOR,
            'the floor of ' + OWN_WRAPPER + ' is not the gate\'s floor; it must '
            'grant the union of what the callee\'s jobs declare'
        )
        self.assertEqual(
            job_permissions(OWN_WRAPPER, 'review-gate'), REVIEW_GATE_FLOOR,
            'the wrapper\'s review-gate job must grant exactly the callee\'s floor'
        )


if __name__ == '__main__':
    unittest.main(verbosity=2)
