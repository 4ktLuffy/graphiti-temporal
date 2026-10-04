"""graphiti-gate: run Graphiti's memory-logic scenarios on any revision, and compare revisions.

    graphiti-gate run    REV [--scenarios DIR] [--repeats 5]
    graphiti-gate diff   BASE HEAD                  # the scorecard for one change
    graphiti-gate triage REV [REV ...] --base main  # many revisions against one base, one table
    graphiti-gate props  REV [REV ...] [--trials 40] # rules on random histories, rates by size
    graphiti-gate audit  [--group G] [--json]        # damage in a live graph; read-only
    graphiti-gate live   REV [REV ...] --repeats 10  # Codex as the judge: pass rates vs base

REV is `main`, a branch or sha, `pr:N` (merged onto the base), or `patch:PATH`. Needs Neo4j
(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD); no model and no API key.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from graphiti_gate.revisions import DEV, ROOT, MergeConflict, prepare

RESULTS = (ROOT if DEV else Path.cwd()) / 'results' / 'gate'
DEFAULT_SCENARIOS = Path(__file__).resolve().parent / 'scenarios'


def run(
    spec: str, scenarios: Path, repeats: int, base: str = 'main', env: dict | None = None
) -> dict:
    """Results for one revision: {'revision', 'label', 'scenarios': {id: result}} or a conflict."""
    try:
        rev = prepare(spec, base=base)
    except MergeConflict as e:  # only a real conflict is a result; other errors must surface
        return {'revision': spec, 'label': spec, 'conflict': str(e), 'scenarios': {}}
    out = subprocess.run(
        [str(rev.python), '-m', 'graphiti_gate.worker', str(scenarios), '--repeats', str(repeats)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    results = {}
    for line in out.stdout.splitlines():
        if line.startswith('{'):
            r = json.loads(line)
            if 'quota_exhausted' in r:
                raise SystemExit(
                    f'*** CODEX CREDITS EXHAUSTED on {spec}: reset the account, then rerun '
                    f'(recorded answers replay for free). {r["quota_exhausted"]}'
                )
            results[r['id']] = r
    if out.returncode != 0 and not results:
        raise RuntimeError(f'worker failed on {spec}: {out.stderr[-1500:]}')
    record = {'revision': spec, 'label': rev.label, 'scenarios': results}
    RESULTS.mkdir(parents=True, exist_ok=True)
    kind, _, rest = spec.partition(':')
    # Never put local paths in result names: a patch or checkout is named by its last component.
    shown = f'{kind}-{Path(rest).name}' if kind in ('patch', 'path') else spec
    name = shown.replace(':', '-').replace('/', '_')
    prefix = 'live-' if (env or {}).get('GATE_JUDGE') == 'live' else ''
    # Results are named by revision and scenario set, so runs on other scenario sets never
    # overwrite them.
    scen = hashlib.sha256(str(Path(scenarios).resolve()).encode()).hexdigest()[:6]
    resolved = Path(scenarios).resolve()
    record['scenario_set'] = (
        str(resolved.relative_to(ROOT)) if resolved.is_relative_to(ROOT) else resolved.name
    )  # never a local absolute path
    (RESULTS / f'{prefix}{name}--{scen}.json').write_text(json.dumps(record, indent=1))
    return record


def fisher_p(a: int, n1: int, b: int, n2: int) -> float:
    """Two-sided Fisher exact p for a/n1 vs b/n2 passes."""
    from math import comb

    total, k = n1 + n2, a + b

    def prob(x: int) -> float:
        return comb(n1, x) * comb(n2, k - x) / comb(total, k)

    observed = prob(a)
    lo, hi = max(0, k - n2), min(k, n1)
    # Relative tolerance: an absolute one swamps very small probabilities.
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= observed * (1 + 1e-9)))


def live_table(base: dict, heads: list[dict]) -> str:
    from graphiti_temporal.stats import wilson

    def rate(r):
        if not r:
            return '', 0, 0
        k, n = r['passes'], r['repeats']
        lo, hi = wilson(k, n)
        return f'{k}/{n} [{lo:.2f}, {hi:.2f}]', k, n

    cols = [base] + heads
    lines = ['| scenario | ' + ' | '.join(c['revision'] for c in cols) + ' |']
    lines.append('|---' * (len(cols) + 1) + '|')
    for i in sorted(base['scenarios']):
        cells = []
        b_text, bk, bn = rate(base['scenarios'].get(i))
        cells.append(b_text)
        for h in heads:
            text, k, n = rate(h['scenarios'].get(i))
            if n and bn:
                p = fisher_p(k, n, bk, bn)
                text += f' p={p:.3f}' if p < 0.2 else ''
            cells.append(text)
        lines.append(f'| `{i}` | ' + ' | '.join(cells) + ' |')
    misses = []
    for c in cols:
        fresh = sum(len(run.get('cache_misses') or []) for r in c['scenarios'].values() for run in (r.get('runs') or []))  # fmt: skip
        misses.append(str(fresh))
    lines.append('| *answers not in the cache (fresh Codex calls)* | ' + ' | '.join(misses) + ' |')
    return '\n'.join(lines)


def _cell(r: dict | None) -> str:
    if r is None:
        return 'missing'
    return {'pass': 'pass', 'fail': 'FAIL', 'flaky': 'flaky', 'error': 'ERROR'}[r['verdict']]


def _change(base: dict | None, head: dict | None) -> str:
    b, h = (base or {}).get('verdict'), (head or {}).get('verdict')
    if (head or {}).get('status') == 'disputed':
        return 'disputed'
    if h in ('flaky', 'error'):
        return h
    if b == h:
        return 'unchanged'
    if h == 'pass':
        return 'fixed'
    return 'broke' if b == 'pass' else 'changed'


def scorecard(base: dict, head: dict) -> str:
    """Markdown a maintainer reads in two minutes."""
    lines = [f'### Memory-logic gate: `{head["label"]}` vs `{base["label"]}`', '']
    if head.get('conflict'):
        return '\n'.join(lines + [f'**Does not merge onto the base:** {head["conflict"][:300]}'])
    ids = sorted(set(base['scenarios']) | set(head['scenarios']))
    changes = {i: _change(base['scenarios'].get(i), head['scenarios'].get(i)) for i in ids}
    counts = {k: sum(v == k for v in changes.values()) for k in ('fixed', 'broke', 'changed', 'flaky', 'error')}  # fmt: skip
    summary = ', '.join(f'**{v} {k}**' for k, v in counts.items() if v) or 'no behaviour change'
    lines += [summary + f' (of {len(ids)} scenarios)', '']
    lines += ['| scenario | base | head | change | why |', '|---|---|---|---|---|']
    order = {
        'broke': 0,
        'error': 1,
        'flaky': 2,
        'fixed': 3,
        'changed': 4,
        'disputed': 5,
        'unchanged': 6,
    }
    for i in sorted(ids, key=lambda i: (order[changes[i]], i)):
        b, h = base['scenarios'].get(i), head['scenarios'].get(i)
        first = (h or {}).get('first', {})
        why = '; '.join(first.get('failures', [])[:2]) or first.get('error', '')
        lines.append(f'| `{i}` | {_cell(b)} | {_cell(h)} | {changes[i]} | {why[:160]} |')
    lines += ['', 'Scenario sources and expected outcomes: `graphiti_gate/scenarios/`. Judge personas and how each '
              'outcome is checked: `graphiti_gate/judges.py`, `graphiti_gate/worker.py`.']  # fmt: skip
    return '\n'.join(lines)


def triage(base: dict, heads: list[dict], reaches: list[dict] | None = None) -> str:
    ids = sorted(base['scenarios'])
    cols = [h['revision'] for h in heads]
    lines = ['| scenario | ' + base['revision'] + ' | ' + ' | '.join(cols) + ' |']
    lines.append('|---' * (len(cols) + 2) + '|')
    for i in ids:
        cells = [_cell(base['scenarios'].get(i))]
        for h in heads:
            cells.append('conflict' if h.get('conflict') else _cell(h['scenarios'].get(i)))
        lines.append(f'| `{i}` | ' + ' | '.join(cells) + ' |')
    if reaches:
        cells = ['']
        for h, r in zip(heads, reaches, strict=True):
            if h.get('conflict') or r is None:
                cells.append('')
            elif r['changed'] == 0:
                cells.append('no core change')
            else:
                tag = ' **untested**' if r['executed'] == 0 else ''
                cells.append(f'{r["executed"]}/{r["changed"]}{tag}')
        lines.append(
            '| *changed `graphiti_core` statements the gate ran* | ' + ' | '.join(cells) + ' |'
        )
    return '\n'.join(lines)


RULES = ('single_current', 'chronology', 'never_extended', 'idempotent')
BUCKETS = ((2, 9), (10, 19), (20, 99))


def props(spec: str, trials: int, max_homes: int, seed: int, base: str = 'main') -> dict:
    rev = prepare(spec, base=base)
    out = subprocess.run(
        [str(rev.python), '-m', 'graphiti_gate.properties', '--trials', str(trials),
         '--max-homes', str(max_homes), '--seed', str(seed)],
        capture_output=True, text=True, env={**os.environ},
    )  # fmt: skip
    rows = [json.loads(line) for line in out.stdout.splitlines() if line.startswith('{')]
    if not rows:
        raise RuntimeError(f'properties failed on {spec}: {out.stderr[-1500:]}')
    record = {'revision': spec, 'label': rev.label, 'trials': rows}
    RESULTS.mkdir(parents=True, exist_ok=True)
    kind, _, rest = spec.partition(':')
    shown = f'{kind}-{Path(rest).name}' if kind in ('patch', 'path') else spec
    name = shown.replace(':', '-').replace('/', '_')
    (RESULTS / f'props-{name}.json').write_text(json.dumps(record, indent=1))
    return record


def props_table(records: list[dict]) -> str:
    from graphiti_temporal.stats import wilson

    lines = ['| rule | homes | ' + ' | '.join(r['revision'] for r in records) + ' |']
    lines.append('|---' * (len(records) + 2) + '|')
    for rule in RULES:
        for lo, hi in BUCKETS:
            cells = []
            for r in records:
                sub = [t for t in r['trials'] if lo <= t['homes'] <= hi]
                k = sum(t[rule] for t in sub)
                a, b = wilson(k, len(sub))
                cells.append(f'{k}/{len(sub)} [{a:.2f}, {b:.2f}]' if sub else '')
            lines.append(f'| {rule} | {lo}-{hi if hi < 99 else "+"} | ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['audit']:
        import asyncio

        from graphiti_gate import audit

        return asyncio.run(audit.main(argv[1:]))
    ap = argparse.ArgumentParser(prog='graphiti-gate', description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('run', 'diff', 'triage', 'props', 'live'):
        p = sub.add_parser(name)
        p.add_argument('revisions', nargs='+')
        p.add_argument('--scenarios', type=Path, default=DEFAULT_SCENARIOS)
        p.add_argument('--repeats', type=int, default=5)
        p.add_argument('--base', default='main')
        p.add_argument('--trials', type=int, default=40)
        p.add_argument('--max-homes', type=int, default=25)
        p.add_argument('--seed', type=int, default=0)
    args = ap.parse_args(argv)
    if 'NEO4J_PASSWORD' not in os.environ:
        print(
            'set NEO4J_PASSWORD (and NEO4J_URI / NEO4J_USER if not the defaults)', file=sys.stderr
        )
        return 2

    if args.cmd == 'live':
        env = {'GATE_JUDGE': 'live', 'GATE_LIVE_CACHE': str(RESULTS / 'live-cache.jsonl')}
        base = run(args.base, args.scenarios, args.repeats, env=env)
        heads = [run(r, args.scenarios, args.repeats, args.base, env=env) for r in args.revisions]
        print(live_table(base, heads))
        return 0
    if args.cmd == 'props':
        records = [
            props(r, args.trials, args.max_homes, args.seed, args.base) for r in args.revisions
        ]
        print(props_table(records))
        return 0
    if args.cmd == 'run':
        for spec in args.revisions:
            rec = run(spec, args.scenarios, args.repeats, args.base)
            for i, r in sorted(rec['scenarios'].items()):
                print(f'{_cell(r):7} {r["passes"]}/{r["repeats"]}  {i}')
        return 0
    base = run(
        args.base if args.cmd == 'triage' else args.revisions[0], args.scenarios, args.repeats
    )
    heads = args.revisions if args.cmd == 'triage' else args.revisions[1:]
    results = [run(h, args.scenarios, args.repeats, args.base) for h in heads]
    if args.cmd == 'diff':
        for h in results:
            print(scorecard(base, h), '\n')
    else:
        from graphiti_gate.reach import reach

        reaches = [None if h.get('conflict') else reach(h['revision'], args.scenarios, args.base) for h in results]  # fmt: skip
        print(triage(base, results, reaches))
    return 0


def main_exit() -> None:
    sys.exit(main())
