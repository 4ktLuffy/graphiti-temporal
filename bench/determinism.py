"""Does Graphiti store the same history every time it is given the same facts?

Crowded back-fill histories (`graphiti_gate.fuzz.crowded`): one person's homes in date order, then
one home back-filled into the middle, its true neighbours worded apart. Each history is ingested
`--runs` times through each path (`resolve_extracted_edges` and `add_triplet`) with the oracle
judge. Per history it records whether the back-filled home always ended where the next began
(`correct`), and how many different end dates it got across all runs (`outcomes`). Node and edge
ids are random per run, so when ranking ties decide the candidates, the result can change.

Runs on Graphiti main, with the runtime fixes installed per `--fixes`:

    NEO4J_PASSWORD=... python bench/determinism.py --fixes none|1|1+3 --out results/determinism-<fixes>.jsonl
"""

import argparse
import asyncio
import json
import logging
from dataclasses import replace

from graphiti_gate.fuzz import crowded, run
from graphiti_temporal import fix


async def history(seed: int, runs: int) -> dict:
    ops = crowded(seed)
    late = ops[-1]
    by_year = sorted(ops, key=lambda o: o.year)
    want = f'{by_year[by_year.index(late) + 1].year}-01-01'
    ends = {}
    for via in ('resolve', 'triplet'):
        ends[via] = []
        for _ in range(runs):
            result = await run([replace(o, via=via) for o in ops], 'neo4j')
            ends[via] += [t[4] for t in result['state'] if t[2] == late.fact]
    every = ends['resolve'] + ends['triplet']
    return {'seed': seed, 'homes': len(ops), 'want': want, 'ends': ends,
            'correct': all(e == want for e in every), 'outcomes': len(set(every)),
            'paths_agree': set(ends['resolve']) == set(ends['triplet'])}  # fmt: skip


async def main() -> None:
    logging.disable(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument('--fixes', choices=['none', '1', '1+3'], default='none')
    ap.add_argument('--seeds', type=int, default=20)
    ap.add_argument('--runs', type=int, default=4)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    if args.fixes in ('1', '1+3'):
        fix.apply_invalidation_filter()
    if args.fixes == '1+3':
        fix.apply_backfill_neighbors()
    rows = []
    with open(args.out, 'w') as f:
        for seed in range(args.seeds):
            rows.append(await history(seed, args.runs))
            f.write(json.dumps(rows[-1]) + '\n')
            f.flush()
    n = len(rows)
    print(f'fixes={args.fixes}: always correct {sum(r["correct"] for r in rows)}/{n}, '
          f'one outcome {sum(r["outcomes"] == 1 for r in rows)}/{n}, '
          f'paths agree {sum(r["paths_agree"] for r in rows)}/{n} '
          f'({args.runs} runs per path per history)')  # fmt: skip


if __name__ == '__main__':
    asyncio.run(main())
