#!/usr/bin/env python3
"""Exercise the collector's real status lookup with paginated gh output."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

COLLECTOR = Path(__file__).resolve().parents[1] / ".github/actions/collect-pr-review-state/action.yml"


def status_lookup():
    text = COLLECTOR.read_text()
    start = text.index('        if ! gh api --paginate --slurp \\\n')
    end = text.index('\n        jq -n --slurpfile t threads.json', start)
    return '\n'.join(line[8:] for line in text[start:end].splitlines()) + '\nprintf "<%s>" "$ROSTER_DESCRIPTION"'


class RosterStatusPagination(unittest.TestCase):
    def run_lookup(self, pages, *, fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mock = root / 'gh'
            mock.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
assert args == ['api', '--paginate', '--slurp',
    'repos/acme/demo/commits/deadbeef/statuses?per_page=100'], args
if os.environ.get('FAIL_STATUS') == '1':
    sys.exit(1)
print(os.environ['STATUS_PAGES'])
''')
            mock.chmod(0o755)
            env = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}',
                       OWNER='acme', REPO='demo', HEAD_SHA='deadbeef',
                       STATUS_PAGES=json.dumps(pages), FAIL_STATUS='1' if fail else '0')
            return subprocess.run(['bash', '-euo', 'pipefail', '-c', status_lookup()],
                                  env=env, cwd=root, capture_output=True, text=True)

    def test_roster_after_first_thirty_and_first_hundred(self):
        unrelated = {'context': 'other', 'description': 'ignored'}
        pages = [[unrelated] * 100,
                 [unrelated] * 30 + [{'context': 'review-roster',
                                      'description': 'asked=[alice] skipped=0'}]]
        proc = self.run_lookup(pages)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, '<asked=[alice] skipped=0>')

    def test_newest_roster_wins_across_pages(self):
        pages = [[{'context': 'review-roster', 'description': 'new'}],
                 [{'context': 'review-roster', 'description': 'old'}]]
        proc = self.run_lookup(pages)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, '<new>')

    def test_no_roster_and_failed_lookup_fall_back_to_absent(self):
        for pages, fail in (([[{'context': 'other', 'description': 'x'}]], False),
                            ([], True)):
            with self.subTest(fail=fail):
                proc = self.run_lookup(pages, fail=fail)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                if fail:
                    self.assertIn('roster=absent', proc.stdout)
                    self.assertTrue(proc.stdout.endswith('<>'))
                else:
                    self.assertEqual(proc.stdout, '<>')


if __name__ == '__main__':
    unittest.main()
