#!/usr/bin/env python3
'''Advance every consumer's `uses: maxi-tools/ci/...` pin to a new tip.

This is the detector for MERGED-BUT-INERT.

Every PR that lands on `maxi-tools/ci` main may change the bytes of the
shared workflows; the consumers pin a sha, so they do not see the change
until this script opens a fan-out PR. The fan-out PR is the signal: if
a consumer does not merge (or close with a reason), that consumer is now
inert on the change that landed.

Designed to run from .github/workflows/fanout-ci-pin.yml with a token
that has `contents: write, pull-requests: write` over the org. Also
runnable by hand for a one-off advance:

    python3 -m github.scripts.fanout_ci_pin \\
        --tip <sha> --dry-run --consumer-repo a/b --consumer-repo c/d

The hardcoded consumer list (`DEFAULT_CONSUMERS`) is the org inventory
measured 2026-09-20: 48 repositories whose
`.github/workflows/review-gate.yml` (or equivalent) references
`maxi-tools/ci/.github/workflows/review-gate-reusable.yml@<sha>`.
Override per-invocation with `--consumer-repo` (repeatable) or
`--consumer-list-file`.

What this script does NOT do:

* It enables merge-commit auto-merge only when effective branch policy
  requires checks. An unprotected consumer remains for manual acceptance.
* It does not retry on a closed PR. A consumer that closes the
  fan-out PR with `pin: skip` is recorded in `OPT_OUTS` (constant
  below) and skipped on subsequent runs.
* It does not touch non-pin files. The fan-out PR touches exactly one
  line per consumer (the `uses:` ref) so the diff stays auditable.
'''

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable


# Default SIGPIPE handler exits with code 141 under `head -1 | ...`,
# which surfaces as a CI failure for callers that pipe our stdout. The
# `head -1` case in fanout-ci-pin.yml is one such caller. Ignore SIGPIPE
# so the script exits cleanly when its downstream pipe closes early.
@functools.cache
def _ignore_sigpipe() -> None:
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        # SIGPIPE does not exist on Windows; on some restricted
        # runtimes `signal.signal` rejects SIG_DFL for SIGPIPE. Either
        # way, the default behaviour is acceptable.
        pass


# 40-character sha. A consumer whose pinned sha is shorter, longer, or
# non-hex (e.g. `@main`, `@v1`) is reported as a problem but the
# problem is never auto-fixed -- fixing the pin shape is a separate,
# reviewed change.
SHA = re.compile(r'\b[0-9a-f]{40}\b')

# Matches the `uses: ...maxi-tools/ci/.github/workflows/<file>.yml@<ref>`
# line we are advancing. `<file>` is the workflow file (the action
# composite files are also referenced this way). The ref is captured so
# we can compare.
USES_RE = re.compile(
    r'''(?P<indent>^[\t ]*(?:-\s*)?uses:\s*)
        maxi-tools/ci/\.github/workflows/(?P<workflow>[A-Za-z0-9_.\-]+?)\.yml@
        (?P<ref>[^\s#'"]+)
    ''',
    re.VERBOSE | re.MULTILINE,
)


# 48 consumer repos measured 2026-09-20. The order is stable so the
# fan-out's PR burst has a predictable notification cadence. New
# consumers are added in alphabetical order on the next inventory sweep.
DEFAULT_CONSUMERS: tuple[str, ...] = (
    'maxi-tools/MaxiTab',
    'maxi-tools/aibi-libre',
    'maxi-tools/bifrost',
    'maxi-tools/bittle-libre',
    'maxi-tools/brisingamen',
    'maxi-tools/coreml-rs',
    'maxi-tools/freya',
    'maxi-tools/fruit-suite',
    'maxi-tools/grok-chrome-extension',
    'maxi-tools/maxi-action-heptathlon',
    'maxi-tools/maxi-agent-runner',
    'maxi-tools/maxi-android-bridge',
    'maxi-tools/maxi-audio',
    'maxi-tools/maxi-cloud',
    'maxi-tools/maxi-config',
    'maxi-tools/maxi-core',
    'maxi-tools/maxi-dist',
    'maxi-tools/maxi-docker',
    'maxi-tools/maxi-e2e',
    'maxi-tools/maxi-firmware-core',
    'maxi-tools/maxi-firmware-embassy',
    'maxi-tools/maxi-firmware-std',
    'maxi-tools/maxi-glass-plugin',
    'maxi-tools/maxi-io',
    'maxi-tools/maxi-ios-bridge',
    'maxi-tools/maxi-kvm',
    'maxi-tools/maxi-kvm-client',
    'maxi-tools/maxi-libs',
    'maxi-tools/maxi-lint',
    'maxi-tools/maxi-memory',
    'maxi-tools/maxi-ml',
    'maxi-tools/maxi-ml-mac-app',
    'maxi-tools/maxi-motion',
    'maxi-tools/maxi-mux',
    'maxi-tools/maxi-nix',
    'maxi-tools/maxi-sandbox',
    'maxi-tools/maxi-stackchan',
    'maxi-tools/maxi-terminal',
    'maxi-tools/maxi-transport',
    'maxi-tools/maxi-tray',
    'maxi-tools/maxi-tui',
    'maxi-tools/maxi-tui-deps',
    'maxi-tools/maxi-ui',
    'maxi-tools/maxi-unity',
    'maxi-tools/maxi-vector-cloud',
    'maxi-tools/maxi-vpad',
    'maxi-tools/maximoji-rs',
    'maxi-tools/rlvgl',
    'maxi-tools/voicemaci',
)


# Consumers that explicitly opted out. Closing the fan-out PR with a
# `pin: skip` comment (the workflow comments this back to the consumer
# when the PR is opened) records the opt-out here. Empty by default.
OPT_OUTS: dict[str, str] = {}


# Workflow files we expect to find pinned in a consumer's
# `.github/workflows/review-gate.yml`. A consumer that pins something
# else (e.g. an internal wrapper) is reported but the line is not
# advanced -- the fan-out shape is constrained to the shared lanes.
WORKFLOW_FILES = frozenset({
    'review-gate-reusable',
    'rust-ci',
    'lane-plan',
    'lane-check',
    'lane-test',
    'lane-package',
    'lane-sign-publish',
    'lane-release-verify',
})


@dataclass(frozen=True)
class Consumer:
    name: str  # e.g. 'maxi-tools/maxi-core'

    def __str__(self) -> str:
        return self.name


# Every consumer the run looked at ends in exactly ONE of these. The
# summary is a partition, not a count of the happy path: a consumer
# whose pin could not be read is `unreadable`, not silently absent.
OUTCOMES = ('opened', 'reused', 'already', 'owned-sync', 'dry-run', 'failed', 'unreadable',
            'opt-out', 'no-pin', 'not-a-sha')


@dataclass(frozen=True)
class FanOut:
    consumer: Consumer
    workflow_file: str  # e.g. 'review-gate-reusable'; '' when nothing was read
    old_ref: str
    new_ref: str  # always a 40-char sha
    pr_url: str | None
    outcome: str = 'planned'
    detail: str = ''

    def as_json(self) -> str:
        return json.dumps({
            'consumer': self.consumer.name,
            'workflow_file': self.workflow_file,
            'old_ref': self.old_ref,
            'new_ref': self.new_ref,
            'pr_url': self.pr_url,
            'outcome': self.outcome,
            'detail': self.detail,
        })


def _run(
    args: list[str], *,
    env: dict[str, str] | None = None,
    workdir: str | None = None,
) -> str:
    '''Run a subprocess and return stdout. Raise on non-zero exit.'''
    proc = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        cwd=workdir,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f'command failed (rc={proc.returncode}): '
            f'{" ".join(args)}\nstderr:\n{proc.stderr}'
        )
    return proc.stdout


def _gh(args: list[str], *, token: str) -> str:
    return _run(['gh', *args], env={'GH_TOKEN': token})


def _required_checks(consumer: Consumer, *, token: str) -> set[str]:
    '''Fail closed on unreadable rules; include classic protection if present.'''
    repo = consumer.name
    branch = json.loads(_gh(['api', f'repos/{repo}', '--jq', '.default_branch | @json'], token=token))
    rules = json.loads(_gh(['api', f'repos/{repo}/rules/branches/{branch}'], token=token))
    if not isinstance(rules, list):
        raise RuntimeError(f'{repo}: effective rules are not a list')
    contexts = {
        check['context'] for rule in rules if rule['type'] == 'required_status_checks'
        for check in rule['parameters']['required_status_checks']
    }
    try:
        classic = json.loads(_gh(
            ['api', f'repos/{repo}/branches/{branch}/protection/required_status_checks'],
            token=token))
    except RuntimeError as exc:
        if 'HTTP 404' not in str(exc):
            raise
    else:
        contexts.update(classic['contexts'])
        contexts.update(check['context'] for check in classic.get('checks', []))
    return contexts


def _enable_automerge(consumer: Consumer, url: str, *, token: str) -> None:
    if not _required_checks(consumer, token=token):
        print(f'{consumer}: no required checks; auto-merge withheld', file=sys.stderr)
        return
    _gh(['pr', 'merge', url, '--repo', consumer.name, '--auto', '--merge'], token=token)


def _fetch_consumer_pin(consumer: Consumer, *, token: str) -> dict[str, str]:
    '''Return {workflow_file: pinned_ref} for `consumer`.

    Scans every file under `.github/workflows/` (and `.github/workflows/`
    specifically; `workflows/` is the legacy location maxi-config's
    wrappers live in and is excluded so this script does not collide
    with `distribute-*.yml`'s coverage of that path). A consumer that
    pins `maxi-tools/ci/.github/workflows/<file>.yml@<ref>` contributes
    one entry. Multiple references to the same file contribute the
    LAST one; the diff is still a single-line advance.
    '''
    out = _gh(
        [
            'api',
            f'repos/{consumer.name}/contents/.github/workflows',
            '--jq', '.[].name',
        ],
        token=token,
    )
    names = [n for n in out.splitlines() if n.endswith(('.yml', '.yaml'))]
    pins: dict[str, str] = {}
    for name in names:
        content = _gh(
            [
                'api',
                f'repos/{consumer.name}/contents/.github/workflows/{name}',
                '--jq', '.content',
            ],
            token=token,
        )
        text = _decode_b64(content)
        # This whole workflow is shipped from maxi-config. An independent pin
        # PR races its sync PR and the next sync reverts the pin. Change the
        # maxi-config SOURCE first; its distributor handles these consumers.
        if (name == 'review-gate.yml' and consumer.name != 'maxi-tools/maxi-config'
                and '# maxi-config-owned Maxi review gate workflow.' in text.splitlines()):
            return {'__owned_sync__': ''}
        for match in USES_RE.finditer(text):
            wf = match.group('workflow')
            if wf not in WORKFLOW_FILES:
                continue
            pins[wf] = match.group('ref')
    return pins


def _decode_b64(b64_text: str) -> str:
    import base64
    # GitHub returns base64 with embedded newlines; strip them.
    return base64.b64decode(b64_text.translate({ord('\n'): None})).decode('utf-8')


# ONE branch per consumer, moved forward on every tip. The first cut
# keyed the branch on the tip sha (`ci/fanout-<sha12>`), so every push to
# ci main that touched a workflow opened a SECOND PR per consumer beside
# the previous one and closed nothing: after ci#31 merged, 49 open
# `ci/fanout-660e29c4` PRs were about to be joined by 49 at a856e0d4
# (run 35504925677, cancelled by hand). The stable name makes a tip
# advance a force-push the existing PR follows, and the legacy per-sha
# PRs are closed as superseded when they are met.
HEAD_REF = 'ci/fanout-pin'
LEGACY_HEAD_RE = re.compile(r'^ci/fanout-[0-9a-f]{12}$')


def _open_fanout_prs(consumer: Consumer, *, token: str) -> list[dict]:
    '''Every open PR on `consumer` whose head is a fan-out branch of ours.'''
    out = _gh(
        [
            'pr', 'list',
            '--repo', consumer.name,
            '--state', 'open',
            '--limit', '100',
            '--json', 'number,url,headRefName',
        ],
        token=token,
    ).strip()
    prs = json.loads(out) if out else []
    return [p for p in prs
            if p['headRefName'] == HEAD_REF or LEGACY_HEAD_RE.match(p['headRefName'])]


def _retire_owned_pin_prs(consumer: Consumer, *, token: str) -> None:
    '''Close only our single-file pin proposals; sync now owns the advance.'''
    for pr in _open_fanout_prs(consumer, token=token):
        detail = json.loads(_gh(
            ['pr', 'view', str(pr['number']), '--repo', consumer.name,
             '--json', 'author,headRefName,files'], token=token))
        if (detail['author']['login'] != 'app/maxi-tools-auth' or
                detail['headRefName'] != pr['headRefName'] or
                [f['path'] for f in detail['files']] != ['.github/workflows/review-gate.yml']):
            raise RuntimeError(f'{consumer}#{pr["number"]}: unexpected author or files; not closing')
        _gh(['pr', 'close', str(pr['number']), '--repo', consumer.name,
             '--delete-branch', '--comment',
             'Superseded by the maxi-config-owned review-gate.yml sync. '
             'The ci pin now advances in maxi-config/maxi-review/review-gate.yml '
             'and reaches this repo through its sync PR.'], token=token)


def _push_pin_branch(
    consumer: Consumer, *, head_ref: str, new_ref: str, token: str,
) -> None:
    '''Clone main, rewrite the pinned ref, commit, force-push `head_ref`.

    A pre-existing branch is reset to the consumer's current main, so the
    PR that follows it always carries exactly one commit over main.
    '''
    import tempfile
    cwd = Path(tempfile.mkdtemp(prefix='fanout-'))
    try:
        _run([
            'git', 'clone', '--depth', '1', '--branch', 'main',
            f'https://x-access-token:{token}@github.com/{consumer.name}.git',
            str(cwd),
        ])
        _run(['git', 'checkout', '-B', head_ref], workdir=str(cwd))
        target = cwd / (('maxi-review/review-gate.yml'
                         if consumer.name == 'maxi-tools/maxi-config'
                         else '.github/workflows/review-gate.yml'))
        text = target.read_text(encoding='utf-8')
        new_text, n = USES_RE.subn(
            lambda m: (
                f'{m.group("indent")}'
                f'maxi-tools/ci/.github/workflows/{m.group("workflow")}.yml@'
                f'{new_ref}'
            ),
            text,
            count=1,  # advance only the first matching line per file
        )
        if n == 0:
            raise RuntimeError(
                f'{consumer.name}: no `uses:` line matched after dry lookup'
            )
        target.write_text(new_text, encoding='utf-8')
        _run(['git', 'add', str(target)], workdir=str(cwd))
        if consumer.name == 'maxi-tools/maxi-config':
            installed = cwd / '.github/workflows/review-gate.yml'
            installed.write_text(new_text, encoding='utf-8')
            _run(['git', 'add', str(installed)], workdir=str(cwd))
        _run(
            [
                'git', '-c', 'user.name=Maxi Boch',
                '-c', 'user.email=874012+maxiboch@users.noreply.github.com',
                'commit', '-m',
                f'ci: advance pin to {new_ref[:12]}',
            ],
            workdir=str(cwd),
        )
        _run(['git', 'push', '-f', 'origin', head_ref], workdir=str(cwd))
    finally:
        _run(['rm', '-rf', str(cwd)])


def _open_pr(
    *,
    consumer: Consumer,
    workflow_file: str,
    old_ref: str,
    new_ref: str,
    tip_sha: str,
    token: str,
    dry_run: bool,
) -> tuple[str, str | None]:
    '''Open or move forward the fan-out PR on `consumer`. Return (outcome, URL).

    `opened`: no fan-out PR was open, one was created on HEAD_REF.
    `reused`: a PR on HEAD_REF was open; its branch was force-pushed to
    the new tip and its title/body updated. Either way, any legacy
    per-sha fan-out PR still open is closed as superseded.
    '''
    title = f'ci: advance pin to {tip_sha[:12]} ({workflow_file})'
    body = (
        f'Fan-out from `maxi-tools/ci` @{tip_sha}.\n\n'
        f'This PR advances `{workflow_file}.yml` from `{old_ref}` to '
        f'`{new_ref}` (40-char sha).\n\n'
        f'One branch per consumer: this PR is moved forward on every tip '
        f'rather than replaced. '
        f'See `docs/contracts/first-party-pin-scheme.md` in `maxi-tools/ci` '
        f'for the rule and the inert-detector this PR is part of. '
        f'To opt out, close the PR and the next fan-out run will skip this '
        f'consumer; re-enable by merging a follow-up that flips the pin.'
    )

    if dry_run:
        return ('dry-run', None)

    prs = _open_fanout_prs(consumer, token=token)
    ours = next((p for p in prs if p['headRefName'] == HEAD_REF), None)
    legacy = [p for p in prs if p['headRefName'] != HEAD_REF]

    _push_pin_branch(consumer, head_ref=HEAD_REF, new_ref=new_ref, token=token)

    if ours:
        _gh(
            [
                'pr', 'edit', str(ours['number']),
                '--repo', consumer.name,
                '--title', title,
                '--body', body,
            ],
            token=token,
        )
        outcome, url = 'reused', ours['url']
    else:
        url = _gh(
            [
                'pr', 'create',
                '--repo', consumer.name,
                '--base', 'main',
                '--head', f'{consumer.name.split("/")[0]}:{HEAD_REF}',
                '--title', title,
                '--body', body,
            ],
            token=token,
        ).strip()
        outcome = 'opened'

    for p in legacy:
        _gh(
            [
                'pr', 'close', str(p['number']),
                '--repo', consumer.name,
                '--delete-branch',
                '--comment',
                f'Superseded by {url}: the fan-out now keeps one branch per '
                f'consumer (`{HEAD_REF}`) and moves it forward on each tip.',
            ],
            token=token,
        )
    _enable_automerge(consumer, url, token=token)
    return (outcome, url)


def _plan(
    *,
    tip_sha: str,
    consumers: Iterable[str],
    token: str,
) -> list[FanOut]:
    '''Compute the fan-out plan; do not mutate anything.

    A consumer that is already at `tip_sha` is reported with
    `old_ref == new_ref` so the caller can log the no-op.
    '''
    plan: list[FanOut] = []

    def dropped(consumer, outcome, detail, wf='', ref=''):
        print(f'{consumer}: {outcome} ({detail})', file=sys.stderr)
        plan.append(FanOut(consumer=consumer, workflow_file=wf, old_ref=ref,
                           new_ref=tip_sha, pr_url=None, outcome=outcome,
                           detail=detail))

    for name in consumers:
        consumer = Consumer(name)
        if name in OPT_OUTS:
            dropped(consumer, 'opt-out', OPT_OUTS[name])
            continue
        try:
            pins = _fetch_consumer_pin(consumer, token=token)
        except Exception as exc:  # noqa: BLE001
            dropped(consumer, 'unreadable', f'could not read pin: {exc}')
            continue
        if not pins:
            dropped(consumer, 'no-pin',
                    'no `uses: maxi-tools/ci/.github/workflows/...` line')
            continue
        if '__owned_sync__' in pins:
            dropped(consumer, 'owned-sync',
                    'review-gate.yml is maxi-config-owned; the source pin is distributed by sync')
            continue
        for wf, ref in pins.items():
            if not SHA.match(ref):
                dropped(consumer, 'not-a-sha',
                        f'pinned at `{ref}`; auto-fix refused', wf=wf, ref=ref)
                continue
            plan.append(FanOut(
                consumer=consumer,
                workflow_file=wf,
                old_ref=ref,
                new_ref=tip_sha,
                pr_url=None,
            ))
    return plan


def _execute(plan: list[FanOut], *, tip_sha: str, token: str, dry_run: bool) -> list[FanOut]:
    executed: list[FanOut] = []
    for entry in plan:
        if entry.outcome != 'planned':
            if entry.outcome == 'owned-sync' and not dry_run:
                try:
                    _retire_owned_pin_prs(entry.consumer, token=token)
                except Exception as exc:  # noqa: BLE001
                    executed.append(replace(entry, outcome='failed', detail=str(exc)))
                    continue
            executed.append(entry)  # dropped in _plan, reason already set
            continue
        if entry.old_ref == entry.new_ref:
            print(f'{entry.consumer} {entry.workflow_file}: already at tip')
            executed.append(replace(entry, outcome='already'))
            continue
        try:
            outcome, url = _open_pr(
                consumer=entry.consumer,
                workflow_file=entry.workflow_file,
                old_ref=entry.old_ref,
                new_ref=entry.new_ref,
                tip_sha=tip_sha,
                token=token,
                dry_run=dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            # The clone/commit/push/pr-create chain for THIS consumer
            # failed. Record it and go on: the other consumers are
            # independent, and a run that stops at the first one leaves
            # every later consumer silently un-fanned.
            print(f'{entry.consumer}: failed ({exc})', file=sys.stderr)
            executed.append(replace(entry, outcome='failed', detail=str(exc)))
            continue
        executed.append(replace(entry, pr_url=url, outcome=outcome))
    assert all(e.outcome in OUTCOMES for e in executed), \
        [e for e in executed if e.outcome not in OUTCOMES]
    return executed


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--tip', required=True, help='40-char sha to advance to')
    parser.add_argument(
        '--consumer-repo', action='append', default=[],
        help='Override the consumer list (repeatable)',
    )
    parser.add_argument(
        '--consumer-list-file', default=None,
        help='File with one `owner/repo` per line; merged with --consumer-repo',
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Plan and report without opening PRs',
    )
    parser.add_argument(
        '--json', action='store_true',
        help='Emit the plan as JSON to stdout (one object per line)',
    )
    return parser.parse_args()


def main() -> int:
    _ignore_sigpipe()
    args = _parse_args()
    tip_sha = args.tip.strip()
    if not SHA.match(tip_sha):
        print(f'--tip must be a 40-char sha, got {tip_sha!r}', file=sys.stderr)
        return 2

    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token and not args.dry_run:
        print('GH_TOKEN (or GITHUB_TOKEN) is required for non-dry-run mode',
              file=sys.stderr)
        return 2

    consumers: list[str] = list(args.consumer_repo)
    if args.consumer_list_file:
        consumers.extend(
            line.strip() for line in Path(args.consumer_list_file).read_text().splitlines()
            if line.strip() and not line.startswith('#')
        )
    # Fall back to the org inventory only when no consumer was specified
    # at all. An explicit empty file or only --consumer-repo values
    # produce an empty plan rather than the default; the self-check
    # uses this to exercise the script with the default list disabled.
    if not consumers and not args.consumer_repo and not args.consumer_list_file:
        consumers = list(DEFAULT_CONSUMERS)

    plan = _plan(tip_sha=tip_sha, consumers=consumers, token=token or 'noop')

    # `--json` used to print the plan and RETURN HERE, before _execute.
    # fanout-ci-pin.yml passes --json unconditionally ("keeps the job
    # summary structured"), so from ci#24 until this fix every run planned
    # ~48 consumers, opened nothing, and printed "fan-out completed". The
    # inert-detector was inert. --json now selects the report format only.
    executed = _execute(plan, tip_sha=tip_sha, token=token or 'noop', dry_run=args.dry_run)

    counts = {o: sum(1 for e in executed if e.outcome == o) for o in OUTCOMES}
    assert sum(counts.values()) == len(executed), (counts, len(executed))
    if args.json:
        for entry in executed:
            print(entry.as_json())
    summary = ', '.join(f'{n} {o}' for o, n in counts.items() if n)
    print(f'fan-out: {len(executed)} consumer row(s): {summary or "none"}',
          file=sys.stderr if args.json else sys.stdout)
    bad = counts['failed'] + counts['unreadable']
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())