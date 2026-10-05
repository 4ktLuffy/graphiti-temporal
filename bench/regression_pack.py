"""Run upstream/tests/test_temporal_regressions.py on main and on each fix PR merged onto main.

    NEO4J_URI=... NEO4J_PASSWORD=... FALKORDB_PORT=... python bench/regression_pack.py

Each test should fail on main and pass on the PR that fixes it. The file is copied into the
revision's checkout, run with that revision's own test helpers, then removed.

FalkorDB's default graph is dropped before each revision. On FalkorDB 4.10.3 (the version this
repository uses, because 6.x breaks Graphiti's index setup, #1947), deleting an indexed edge can
leave its index entry behind, and a new edge that reuses its id then matches range queries it
should not (6.0.1 is correct). Graphiti's test cleanup deletes edges, so results would depend on
what ran before.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from graphiti_gate.revisions import prepare

ROOT = Path(__file__).resolve().parent.parent
TEST = ROOT / 'upstream' / 'tests' / 'test_temporal_regressions.py'
OUT = ROOT / 'results' / 'regression_pack.jsonl'


def fresh_falkordb() -> None:
    if 'FALKORDB_PORT' not in os.environ:
        return
    from falkordb import FalkorDB

    db = FalkorDB(
        host=os.environ.get('FALKORDB_HOST', 'localhost'), port=int(os.environ['FALKORDB_PORT'])
    )
    if 'default_db' in db.list_graphs():
        db.select_graph('default_db').delete()


def run(spec: str) -> list[dict]:
    rev = prepare(spec)
    fresh_falkordb()
    target = rev.worktree / 'tests' / TEST.name
    report = rev.worktree / 'pack-report.xml'
    # The revision's own environment, plus what Graphiti's tests import.
    subprocess.run(['uv', 'pip', 'install', '-q', '--python', str(rev.python),
                    'pytest', 'pytest-asyncio', 'falkordb', 'numpy'], check=True)  # fmt: skip
    shutil.copy(TEST, target)
    try:
        t0 = time.time()
        out = subprocess.run([str(rev.python), '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
                        f'--junitxml={report}', f'tests/{TEST.name}'],
                       cwd=rev.worktree, capture_output=True, text=True, env=os.environ)  # fmt: skip
        seconds = round(time.time() - t0, 1)
        if not report.exists():
            raise RuntimeError(
                f'pytest produced no report on {spec}: {out.stdout[-2000:]}{out.stderr[-2000:]}'
            )
        cases = ET.parse(report).getroot().iter('testcase')
        return [{'revision': spec, 'sha': (rev.head_sha or rev.base_sha)[:7], 'test': c.get('name'),
                 'outcome': 'fail' if c.find('failure') is not None or c.find('error') is not None
                 else 'skip' if c.find('skipped') is not None else 'pass',
                 'seconds': float(c.get('time')), 'file_seconds': seconds} for c in cases]  # fmt: skip
    finally:
        target.unlink(missing_ok=True)
        report.unlink(missing_ok=True)


def main() -> None:
    specs = sys.argv[1:] or [
        'main',
        'pr:1596',
        'pr:1957',
        'patch:bench/patches/01+03-neighbors.patch',
    ]
    rows = [r for spec in specs for r in run(spec)]
    OUT.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    tests = sorted({r['test'] for r in rows}, key=str)
    specs = list(dict.fromkeys(r['revision'] for r in rows))
    print('| test | ' + ' | '.join(specs) + ' |\n|---' * 1 + '|---' * len(specs) + '|')
    for t in tests:
        cells = [
            next(r['outcome'] for r in rows if r['test'] == t and r['revision'] == s) for s in specs
        ]
        print(f'| `{t}` | ' + ' | '.join(cells) + ' |')
    for s in specs:
        print(s, 'file runtime', next(r['file_seconds'] for r in rows if r['revision'] == s), 's')


if __name__ == '__main__':
    main()
