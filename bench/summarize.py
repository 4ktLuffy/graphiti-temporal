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


if __name__ == '__main__':
    history_depth()
    as_of()
    real_llm()
    fulltext_cost()
