"""Every table in README.md and FINDINGS.md, regenerated from results/*.jsonl.

uv run python bench/summarize.py
"""

from __future__ import annotations

import json
from collections import defaultdict
from math import comb
from pathlib import Path

from graphiti_temporal.stats import wilson

RESULTS = Path(__file__).resolve().parent.parent / 'results'


def rows(name: str) -> tuple[dict, list[dict]]:
    lines = [json.loads(line) for line in (RESULTS / name).read_text().splitlines() if line]
    return lines[0]['meta'], lines[1:]


def rate(k: int, n: int) -> str:
    lo, hi = wilson(k, n)
    return f'{k}/{n} [{lo:.2f}, {hi:.2f}]'


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p for b and c discordant pairs."""
    n, k = b + c, min(b, c)
    if n == 0:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2**n)


def history_depth() -> None:
    print('## Oracle: current home retired, by past homes (Neo4j 5.26.2, 30 seeds)\n')
    arms = {'none': 'no fix', '1': 'fix 1', '2': 'fix 2 only (control)', 'both': 'fix 1+2'}
    table: dict[int, dict[str, str]] = defaultdict(dict)
    for fix, label in arms.items():
        path = RESULTS / f'history_depth_fix-{fix}.jsonl'
        if not path.exists():
            continue
        _, rs = rows(path.name)
        for n in sorted({r['n_history'] for r in rs}):
            sub = [r for r in rs if r['n_history'] == n]
            table[n][label] = rate(sum(r['live_retired'] for r in sub), len(sub))
    labels = [lab for lab in arms.values() if any(lab in v for v in table.values())]
    print('| past homes | ' + ' | '.join(labels) + ' |')
    print('|---' * (len(labels) + 1) + '|')
    for n, cells in sorted(table.items()):
        print(f'| {n} | ' + ' | '.join(cells.get(lab, '') for lab in labels) + ' |')
    print()


def as_of() -> None:
    print('## As-of search finds the fact true at T (Neo4j, 20 seeds)\n')
    print('| past homes | when | channel | no fix | fix 2 |')
    print('|---|---|---|---|---|')
    _, base = rows('as_of_fix-none.jsonl')
    _, fixed = rows('as_of_fix-2.jsonl')
    keys = sorted({(r['n_history'], r['when'], r['channel']) for r in base})
    for key in keys:
        cells = []
        for rs in (base, fixed):
            sub = [r for r in rs if (r['n_history'], r['when'], r['channel']) == key]
            cells.append(rate(sum(r['found'] for r in sub), len(sub)))
        print(f'| {key[0]} | {key[1]} | {key[2]} | {cells[0]} | {cells[1]} |')
    print()


def real_llm() -> None:
    print('## Real model through add_episode: current home retired (paired by seed)\n')
    print('| past homes | model | no fix | fix 1 | pairs fix-only / base-only | exact McNemar p |')
    print('|---|---|---|---|---|---|')
    for depth in (20, 50):
        meta, base = rows(f'real_llm_h{depth}_fix-none.jsonl')
        _, fixed = rows(f'real_llm_h{depth}_fix-1.jsonl')
        b = {r['seed']: r['live_retired'] for r in base if r['error'] is None}
        f = {r['seed']: r['live_retired'] for r in fixed if r['error'] is None}
        seeds = sorted(b.keys() & f.keys())
        fix_only = sum(f[s] and not b[s] for s in seeds)
        base_only = sum(b[s] and not f[s] for s in seeds)
        print(
            f'| {depth} | {meta["llm"]} | {rate(sum(b[s] for s in seeds), len(seeds))} | '
            f'{rate(sum(f[s] for s in seeds), len(seeds))} | {fix_only} / {base_only} | '
            f'{mcnemar_exact(fix_only, base_only):.4f} |'
        )
    print()


def fulltext_cost() -> None:
    print('## Cost of returning every fulltext hit (Neo4j, median of 15 warm runs)\n')
    print('| matching facts | form | median ms | valid rows returned (of 20) |')
    print('|---|---|---|---|')
    _, rs = rows('fulltext_cost.jsonl')
    for r in rs:
        print(f'| {r["facts"]} | {r["form"]} | {r["median_ms"]} | {r["rows_returned"]} |')
    print()


def realistic() -> None:
    print('## Realistic retrieval: semantic embedder, 6 wordings, 4 people (paired by seed)\n')
    print('| run | current home retired | shown to the model | collateral | median tokens |')
    print('|---|---|---|---|---|')
    arms = {}
    for path in sorted((RESULTS / 'realistic').glob('*.jsonl')):
        meta, rs = rows(f'realistic/{path.name}')
        arms[path.stem] = {r['seed']: r for r in rs}
        ok = [r for r in rs if r['error'] is None]
        tokens = sorted(r['tokens'] for r in ok)
        median = (tokens[len(tokens) // 2] + tokens[(len(tokens) - 1) // 2]) / 2
        print(f'| {path.stem} | {rate(sum(r["retired"] for r in ok), len(ok))} | '
              f'{sum(r["shown"] for r in ok)}/{len(ok)} | {sum(bool(r["collateral"]) for r in ok)} | {median:,.0f} |')  # fmt: skip
    for base in [k for k in arms if k.endswith('-fixnone')]:
        fixed = base.replace('-fixnone', '-fix1')
        if fixed in arms:
            a, b = arms[base], arms[fixed]
            only_fix = sum(b[s]['retired'] and not a[s]['retired'] for s in a if s in b)
            only_base = sum(a[s]['retired'] and not b[s]['retired'] for s in a if s in b)
            print(
                f'\n{base} vs {fixed}: {only_fix} fix-only / {only_base} base-only, '
                f'exact McNemar p = {mcnemar_exact(only_fix, only_base):.2g}'
            )
    print()


def backfill() -> None:
    print('## Back-fill chronology by history size (oracle judge, Neo4j)\n')
    # Graphiti main b7fc30f. Up to 30 homes: the 60 histories of seed 0; above: the 30 of seed 1.
    runs = {
        'fix 1': ['gate/props-patch-01-invalidation-candidates.patch.json', None],
        'fix 1 + top 30': ['gate/props-patch-01+topk30.patch-max30.json',
                           'gate/props-patch-01+topk30.patch-max90.json'],
        'fix 1 + fix 3': ['gate/props-patch-01+03-neighbors.patch--t60-h30-s0.json',
                          'gate/props-patch-01+03-neighbors.patch--t30-h90-s1.json'],
    }  # fmt: skip
    buckets = [(10, 19), (20, 30), (31, 60), (61, 90)]
    print('| homes | ' + ' | '.join(runs) + ' |')
    print('|---' * (len(runs) + 1) + '|')

    def load(f):
        return json.loads((RESULTS / f).read_text())['trials'] if f else []

    trials = {k: [*[t for t in load(v[0]) if t['homes'] <= 30], *[t for t in load(v[1]) if t['homes'] > 30]]
              for k, v in runs.items()}  # fmt: skip
    for lo, hi in buckets:
        cells = []
        for k in runs:
            sub = [t for t in trials[k] if lo <= t['homes'] <= hi]
            cells.append(f'{sum(t["chronology"] for t in sub)}/{len(sub)}' if sub else '')
        print(f'| {lo}-{hi} | ' + ' | '.join(cells) + ' |')
    print()


def fix3_cost() -> None:
    print('## Fix 3 lookup cost (patch function vs reading the whole history, p50 ms)\n')
    print('| backend | facts | neighbours | read all |')
    print('|---|---|---|---|')
    for line in (RESULTS / 'neighbors_cost.jsonl').read_text().splitlines():
        r = json.loads(line)
        print(
            f'| {r["backend"]} | {r["facts"]:,} | {r["neighbors_p50_ms"]} | {r["read_all_p50_ms"]} |'
        )
    print()


def multivalued() -> None:
    print('## Many true values: 12 open likes, one back-filled (20 trials)\n')
    print('| run | judge | existing likes ended, mean | new like ended |')
    print('|---|---|---|---|')
    for path in sorted(RESULTS.glob('multivalued-*.jsonl')):
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if 'mean_ended_existing' in r:
                print(
                    f'| {path.stem.removeprefix("multivalued-")} | {r["persona"]} | '
                    f'{r["mean_ended_existing"]:.2f} | {r["new_ended"]}/{r["trials"]} |'
                )
    print()


def determinism() -> None:
    print('## Same facts, same history: 20 crowded back-fills, 4 runs per path\n')
    print('| fixes | always correct | one outcome | both paths agree |')
    print('|---|---|---|---|')
    for fixes in ('none', '1', '1+3'):
        path = RESULTS / f'determinism-{fixes}.jsonl'
        if path.exists():
            rs = [json.loads(line) for line in path.read_text().splitlines()]
            n = len(rs)
            print(
                f'| {fixes} | {sum(r["correct"] for r in rs)}/{n} | '
                f'{sum(r["outcomes"] == 1 for r in rs)}/{n} | {sum(r["paths_agree"] for r in rs)}/{n} |'
            )
    print()


def batch() -> None:
    print('## Two adjacent old homes, one at a time or in one episode (20 seeds, oracle)\n')
    print('| fixes | one at a time | in one episode |')
    print('|---|---|---|')
    for fixes in ('none', '1', '1+3'):
        path = RESULTS / f'batch-{fixes}.jsonl'
        if path.exists():
            rs = [json.loads(line) for line in path.read_text().splitlines()]
            cell = [f'{sum(r["correct"] for r in rs if r["batched"] == b)}/{sum(r["batched"] == b for r in rs)}'
                    for b in (False, True)]  # fmt: skip
            print(f'| {fixes} | {cell[0]} | {cell[1]} |')
    print()


if __name__ == '__main__':
    history_depth()
    as_of()
    real_llm()
    fulltext_cost()
    realistic()
    backfill()
    fix3_cost()
    multivalued()
    determinism()
    batch()
