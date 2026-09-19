#!/usr/bin/env python3
'''Check the `uses:` references in this repository.

WHY THIS REPOSITORY NEEDS IT MOST
---------------------------------
Every workflow here is a `workflow_call` reusable. None of them runs on a
pull request, so until now a change landed with three review-bot checks and
nothing that read the YAML. This repository defines CI for ~48 consumers; it
had none of its own.

Two properties, both chosen because breaking them fails somewhere other than
here:

1. A relative `uses: ./.github/workflows/lane-x.yml` resolves INSIDE this
   repository at the sha a consumer pinned. Rename a lane and every consumer
   breaks at once, on a revision this repository has already moved past, with
   no local signal at all.

2. An external action is pinned to a 40-character sha. A tag is mutable, and
   a mutable reference in a reusable workflow that ~48 repositories execute is
   a supply-chain hole. Same-owner references are exempt: `maxi-tools/*` is
   this org, and pinning across it is a different trade (see maxi-config
   docs/contracts/mirror-pin-is-a-security-boundary.md).

Run it directly: `python3 .github/scripts/check_workflow_refs.py`
'''

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'
ACTIONS = ROOT / '.github/actions'

# `uses:` on an active line. Comments are stripped first, because a sha in a
# comment satisfying a pin check while the active line says something else is
# the "a comment is not a guard" failure this org has already been bitten by.
USES = re.compile(r'^\s*(?:-\s*)?uses:\s*([^\s#]+)')
SHA = re.compile(r'^[0-9a-f]{40}$')
# `path:` on an `actions/checkout` step. A relative `uses:` under one of these
# is materialised at RUNTIME and cannot be resolved in this tree.
CHECKOUT_PATH = re.compile(r'^\s*path:\s*([^\s#]+)')


def active_lines(text):
    for raw in text.splitlines():
        line = raw.split(' #', 1)[0].rstrip() if ' #' in raw else raw.rstrip()
        if line.strip().startswith('#') or not line.strip():
            continue
        yield line


def yaml_files():
    for base in (WORKFLOWS, ACTIONS):
        if base.exists():
            for path in sorted(base.rglob('*.yml')):
                yield path
            for path in sorted(base.rglob('*.yaml')):
                yield path


def runtime_paths(text):
    '''Directories a checkout step creates during the run.

    `lane-plan.yml` checks maxi-config out into `.maxi-config` with a sparse
    checkout and then calls `./.maxi-config/.github/actions/resolve-runner`.
    That is correct and cannot resolve in this tree, so the exemption is
    DERIVED from the workflow rather than hardcoded -- hardcoding the name
    would also excuse a typo of it.
    '''
    out = set()
    for line in active_lines(text):
        match = CHECKOUT_PATH.match(line)
        if match:
            out.add(match.group(1).strip('"').strip("'").strip('/'))
    return out


def main():
    problems = []
    checked = 0
    deferred = []
    for path in yaml_files():
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding='utf-8')
        fetched = runtime_paths(text)
        for line in active_lines(text):
            match = USES.match(line)
            if match is None:
                continue
            ref = match.group(1).strip('"').strip("'")
            checked += 1

            if ref.startswith('./'):
                inner = ref[2:]
                root_dir = inner.split('/', 1)[0]
                if root_dir in fetched:
                    # Materialised by a checkout step in this same workflow.
                    deferred.append(f'{rel}: {ref} (created at runtime)')
                    continue
                if not (ROOT / inner).is_file():
                    problems.append(
                        f'{rel}: `uses: {ref}` does not resolve, and no '
                        f'checkout step in this workflow creates '
                        f'{root_dir!r}. Consumers execute this call inside '
                        f'THIS repository at the sha they pinned, so a '
                        f'missing target breaks all of them at once and '
                        f'nothing here reports it.')
                continue

            if '@' not in ref:
                problems.append(f'{rel}: `uses: {ref}` carries no ref at all')
                continue

            name, _, version = ref.rpartition('@')
            if name.startswith('maxi-tools/'):
                # Same owner. Pinning across the org is a separate decision
                # with its own contract; this check does not relitigate it.
                continue
            if not SHA.match(version):
                problems.append(
                    f'{rel}: `uses: {ref}` is not pinned to a 40-character '
                    f'sha. A tag is mutable, and ~48 repositories execute '
                    f'this file.')

    # A checker that silently found nothing to check passes forever. Say the
    # denominator out loud -- an empty scan is how a false clean is produced.
    for note in deferred:
        # Printed, never silent: an exemption nobody sees is how the next
        # unresolvable reference gets waved through as one of these.
        print(f'deferred to runtime: {note}')
    print(f'checked {checked} `uses:` references across '
          f'{len(list(yaml_files()))} files '
          f'({len(deferred)} resolved by a runtime checkout)')
    if checked == 0:
        print('ERROR: no `uses:` references found at all; this scan measured '
              'nothing and must not be read as a pass', file=sys.stderr)
        return 1

    for problem in problems:
        print(f'ERROR: {problem}', file=sys.stderr)
    if problems:
        print(f'{len(problems)} problem(s)', file=sys.stderr)
        return 1
    print('all references resolve and all external actions are sha-pinned')
    return 0


if __name__ == '__main__':
    sys.exit(main())
