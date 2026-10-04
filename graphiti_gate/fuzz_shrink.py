"""Shrink a fuzz finding to the smallest workload that still produces it, and save it as a case.

    python -m graphiti_gate.fuzz_shrink results/fuzz/run-1.jsonl --out graphiti_gate/fuzz_cases

For each seed with findings, operations are removed one at a time while a finding of the same
kind (oracle, backend, and which rule failed) still appears on two consecutive runs. The minimal workload is written
as JSON to the cases directory, which `--replay` in this module runs. Cases live outside the
scenario set, so the frozen gate (PROSPECTIVE.md) is untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from graphiti_gate.fuzz import Op, trial_ops


def kind(finding: dict) -> str:
    """Which rule failed, so one known bug cannot stand in for another while shrinking."""
    detail = str(finding['detail'])
    if finding['oracle'] != 'rule':
        return finding['oracle']
    if detail.startswith('crash'):
        return 'crash'
    if ' or after ' in detail:
        return 'or-date-search'
    if detail.startswith('search'):
        return 'as-of-search'
    return 'two-current' if 'current values' in detail else 'interval-chain'


def _signature(finding: dict) -> tuple[str, str, str]:
    return finding['oracle'], finding['backend'].split(' vs ')[0], kind(finding)


async def _reproduces(
    ops: list[Op], signature: tuple[str, str, str], backends, concurrency
) -> dict | None:
    """The matching finding if it appears on two consecutive runs, else None."""
    match = None
    for _ in range(2):
        result = await trial_ops(ops, backends, concurrency)
        hits = [f for f in result['findings'] if _signature(f) == signature]
        if not hits:
            return None
        match = hits[0]
    return match


async def shrink(ops: list[Op], signature, backends, concurrency) -> tuple[list[Op], dict | None]:
    finding = await _reproduces(ops, signature, backends, concurrency)
    if finding is None:
        return ops, None
    changed = True
    while changed and len(ops) > 1:
        changed = False
        for i in range(len(ops)):
            candidate = ops[:i] + ops[i + 1 :]
            hit = await _reproduces(candidate, signature, backends, concurrency)
            if hit is not None:
                ops, finding, changed = candidate, hit, True
                break
    return ops, finding


async def main() -> None:
    logging.disable(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument('run', type=Path)
    ap.add_argument('--out', type=Path, default=Path('graphiti_gate/fuzz_cases'))
    ap.add_argument('--limit', type=int, default=20, help='at most this many distinct signatures')
    ap.add_argument('--kind', help='only findings of this kind, e.g. interval-chain')
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(line) for line in args.run.read_text().splitlines() if line.startswith('{')]
    done: set[tuple[str, str, str]] = set()
    for row in rows:
        for f in row['findings']:
            sig = _signature(f)
            if sig in done or len(done) >= args.limit or (args.kind and sig[2] != args.kind):
                continue
            ops = [Op(**o) for o in row['ops']]
            backends = sorted({sig[1], 'neo4j'}) if sig[0] == 'backends' else [sig[1]]
            if sig[0] == 'backends':
                backends = sorted({b for b in f['backend'].split(' vs ')})
            small, finding = await shrink(ops, sig, backends, sig[0] == 'concurrency')
            if finding is None:
                print(
                    json.dumps({'seed': row['seed'], 'signature': sig, 'reproduced': False}),
                    flush=True,
                )
                continue
            done.add(sig)
            case = {'signature': list(sig), 'from_seed': row['seed'], 'ops_before': len(ops),
                    'ops': [o.__dict__ for o in small], 'backends': backends,
                    'concurrency': sig[0] == 'concurrency', 'finding': finding}  # fmt: skip
            name = f'{sig[2]}-{sig[1]}-seed{row["seed"]}.json'
            (args.out / name).write_text(json.dumps(case, indent=1, default=str))
            print(json.dumps({'case': name, 'ops': f'{len(ops)} -> {len(small)}', 'finding': str(finding['detail'])[:200]}), flush=True)  # fmt: skip


if __name__ == '__main__':
    asyncio.run(main())
