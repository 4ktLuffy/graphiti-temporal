"""Would the gate have caught real Graphiti bugs before they were fixed?

For each fix commit F on Graphiti's main since Sept 2025 that touched memory logic, the gate's
scenarios (frozen: none was written for these bugs) run on F's parent and on F. A scenario that
passes on F and fails on F^ means the gate would have flagged the bug before it shipped. At F^,
Graphiti's own test suite was green on main while the bug existed (that is how it shipped), so every
catch is a bug their suite missed at the time. Old versions that the gate cannot run are reported as
such, not as misses.

    NEO4J_PASSWORD=... uv run python bench/real_regressions.py
"""

from __future__ import annotations

import json
import subprocess
import sys

from graphiti_gate import cli
from graphiti_gate.revisions import MIRROR, ensure_mirror

FIXES = [  # (commit, title) from `git log --since=2025-09-01` on files the gate exercises
    ('73015e9', 'Fix datetime comparison errors by normalizing to UTC (#988)'),
    ('420676f', 'fix: Prevent duplicate edge facts within same episode (#955)'),
    ('d7828d4', 'Fix index out of range errors in LLM deduplication responses (#939)'),
    ('5ca8b95', 'fix: Improve deduplication ID validation and logging (#965)'),
    ('5902825', 'fix: Improve edge extraction entity ID validation (#968)'),
    ('3fcd587', 'fix: Add edge type validation based on node labels (#948)'),
    ('d5c4fc0', 'Fix limited number of edges (#1124)'),
    (
        '45a3d92',
        'fix(edges): preserve all signatures when edge type is reused across node pairs (#1197)',
    ),
    ('fe19482', 'fix(summary): exclude duplicate edges from node summary generation (#1223)'),
    ('c1afd8b', 'fix: extract custom edge attributes on first episode ingestion (#1242)'),
    ('993e081', 'fix(attributes): preserve prior node attributes when no entity type applies'),
    ('d9a2db9', 'Consolidate overlapping backend fixes (#1695)'),
    ('3ff5c16', 'Land contributor fixes from #1686 #1689 #1720 #1761 (#1856)'),
]
OUT = cli.RESULTS / 'real-regressions.jsonl'


def main() -> None:
    ensure_mirror()
    rows = []
    for sha, title in FIXES:
        parent = subprocess.run(['git', 'rev-parse', '--short', f'{sha}^'], cwd=MIRROR,
                                capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
        try:
            before = cli.run(parent, cli.DEFAULT_SCENARIOS, repeats=2)
            after = cli.run(sha, cli.DEFAULT_SCENARIOS, repeats=2)
        except Exception as e:  # an old revision the gate cannot install or run is a result
            rows.append({'fix': sha, 'title': title, 'runnable': False, 'error': str(e)[:300]})
            print(json.dumps(rows[-1]), flush=True)
            continue
        runnable = any(r['verdict'] in ('pass', 'fail') for r in before['scenarios'].values())
        caught = sorted(
            i for i, r in after['scenarios'].items()
            if r['verdict'] == 'pass' and before['scenarios'].get(i, {}).get('verdict') in ('fail', 'error')
            and r['status'] != 'disputed'
        )  # fmt: skip
        errors_before = sum(r['verdict'] == 'error' for r in before['scenarios'].values())
        rows.append({'fix': sha, 'parent': parent, 'title': title, 'runnable': runnable,
                     'caught_by': caught, 'errors_before': errors_before,
                     'errors_after': sum(r['verdict'] == 'error' for r in after['scenarios'].values())})  # fmt: skip
        print(json.dumps(rows[-1]), flush=True)
    OUT.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    caught = [r for r in rows if r.get('caught_by')]
    print(f'gate would have caught {len(caught)} of {len(rows)} real fixes before they shipped')


if __name__ == '__main__':
    sys.exit(main())
