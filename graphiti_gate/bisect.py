"""Find the commit that changed a scenario's verdict, by binary search over Graphiti's history.

    python -m graphiti_gate.bisect SCENARIO.yaml --good 27d4f10 --bad main

`good` is a revision where the scenario passes and `bad` one where it fails. Only first-parent
commits on main that touch graphiti_core are tested. A commit the gate cannot run (install
failure, or the scenario errors) is skipped and its neighbour tried, as `git bisect skip` does.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from graphiti_gate import cli
from graphiti_gate.revisions import MIRROR, ensure_mirror


def verdict(sha: str, scenario: Path, repeats: int) -> str:
    try:
        record = cli.run(sha, scenario, repeats)
    except Exception:  # an uninstallable commit is skipped, not judged
        return 'skip'
    results = list(record['scenarios'].values())
    if not results or results[0]['verdict'] in ('error', 'flaky'):
        return 'skip'
    return results[0]['verdict']


def bisect(scenario: Path, good: str, bad: str, repeats: int = 2) -> dict:
    ensure_mirror()
    commits = subprocess.run(
        ['git', 'rev-list', '--first-parent', '--reverse', f'{good}..{bad}', '--', 'graphiti_core'],
        cwd=MIRROR, capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    log = []
    lo, hi = -1, len(commits) - 1  # commits[lo] passes (lo=-1 is `good`), commits[hi] fails
    skipped: set[int] = set()
    while True:
        untested = [i for i in range(lo + 1, hi) if i not in skipped]
        if not untested:
            break  # adjacent, or everything between them could not be run
        probe = untested[len(untested) // 2]
        v = verdict(commits[probe], scenario, repeats)
        log.append({'commit': commits[probe][:7], 'verdict': v})
        print(json.dumps(log[-1]), flush=True)
        if v == 'skip':
            skipped.add(probe)
        elif v == 'pass':
            lo = probe
        else:
            hi = probe
    unresolved = [commits[i][:7] for i in range(lo + 1, hi)]
    first_bad = commits[hi]
    subject = subprocess.run(['git', 'log', '-1', '--format=%h %ad %s', '--date=short', first_bad],
                             cwd=MIRROR, capture_output=True, text=True).stdout.strip()  # fmt: skip
    return {'scenario': str(scenario.name), 'first_bad': subject, 'tested': log,
            'unrunnable_between': unresolved,
            'candidates': len(commits), 'last_good': commits[lo][:7] if lo >= 0 else good}  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('scenario', type=Path)
    ap.add_argument('--good', required=True)
    ap.add_argument('--bad', default='main')
    ap.add_argument('--repeats', type=int, default=2)
    args = ap.parse_args()
    result = bisect(args.scenario, args.good, args.bad, args.repeats)
    print(json.dumps(result, indent=1))


if __name__ == '__main__':
    main()
