"""The prospective test: does the frozen gate catch Graphiti bugs fixed after it was frozen?

Pre-registered in PROSPECTIVE.md, whose commit gives the freeze a public timestamp. Every commit on
getzep/graphiti main after BASE whose message says fix and that touches the code the gate
exercises is tested exactly like bench/real_regressions.py: the frozen scenarios on the fix's
parent and on the fix. `--check-freeze` verifies the gate has not changed since the freeze.

    NEO4J_PASSWORD=... uv run python bench/prospective.py [--check-freeze]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FROZEN = ['graphiti_gate/judges.py', 'graphiti_gate/worker.py', 'graphiti_gate/vias.py',
          'graphiti_gate/scenario.py', 'graphiti_gate/compat.py']  # fmt: skip
WATCHED = ['graphiti_core/utils/maintenance/edge_operations.py',
           'graphiti_core/utils/maintenance/node_operations.py',
           'graphiti_core/utils/maintenance/dedup_helpers.py', 'graphiti_core/utils/bulk_utils.py',
           'graphiti_core/search/search_filters.py', 'graphiti_core/search/search_utils.py']  # fmt: skip


def gate_fingerprint() -> str:
    h = hashlib.sha256()
    files = sorted((ROOT / 'graphiti_gate' / 'scenarios').rglob('*.yaml')) + [
        ROOT / f for f in FROZEN
    ]
    for f in files:
        h.update(str(f.relative_to(ROOT)).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--check-freeze', action='store_true')
    args = ap.parse_args()
    spec = json.loads((ROOT / 'results/gate/prospective_commitment.json').read_text())
    if gate_fingerprint() != spec['gate_fingerprint']:
        print('the gate changed since the freeze; check out the freeze commit to run this test')
        return 1
    if args.check_freeze:
        print('gate unchanged since the freeze')
        return 0
    from graphiti_gate import cli
    from graphiti_gate.revisions import MIRROR, ensure_mirror

    ensure_mirror()
    log = subprocess.run(
        ['git', 'log', '--first-parent', '--format=%h %s', f"{spec['graphiti_base']}..origin/main", '--', *WATCHED],
        cwd=MIRROR, capture_output=True, text=True, check=True,
    ).stdout.splitlines()  # fmt: skip
    fixes = [line.split(' ', 1) for line in log if 'fix' in line.lower()]
    rows = []
    for sha, title in fixes:
        parent = subprocess.run(['git', 'rev-parse', '--short', f'{sha}^'], cwd=MIRROR,
                                capture_output=True, text=True, check=True).stdout.strip()  # fmt: skip
        before = cli.run(parent, cli.DEFAULT_SCENARIOS, 2)
        after = cli.run(sha, cli.DEFAULT_SCENARIOS, 2)
        caught = sorted(i for i, r in after['scenarios'].items() if r['verdict'] == 'pass'
                        and before['scenarios'].get(i, {}).get('verdict') in ('fail', 'error'))  # fmt: skip
        rows.append({'fix': sha, 'title': title, 'caught_by': caught})
        print(json.dumps(rows[-1]), flush=True)
    print(
        f'frozen gate caught {sum(bool(r["caught_by"]) for r in rows)} of {len(rows)} fixes merged after the freeze'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
