"""Does Graphiti retire Alice's current home when she moves, as her history grows?

Runs Graphiti's own `resolve_extracted_edges` against Neo4j, with an oracle in place of the LLM.
Prints, per history depth, how often the current home was shown to the model and retired.

    uv run python bench/history_depth.py --depths 0 5 9 10 12 20 50 --seeds 30
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.tracer import NoOpTracer
from graphiti_core.utils.maintenance import edge_operations

from graphiti_temporal import world as W
from graphiti_temporal.stats import wilson
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, OracleLLM


async def trial(driver, n_history: int, seed: int) -> dict:
    oracle = OracleLLM()
    clients = GraphitiClients(
        driver=driver,
        llm_client=oracle,
        embedder=HashEmbedder(),
        cross_encoder=NullCrossEncoder(),
        tracer=NoOpTracer(),
    )
    world = await W.build(driver, oracle, n_history=n_history, seed=seed)
    edge, episode, entities = await W.new_move(driver, oracle, world)
    _, invalidated, _ = await edge_operations.resolve_extracted_edges(
        clients, [edge], episode, entities, {}, {}
    )
    shown = oracle.calls[-1].invalidation_candidates + oracle.calls[-1].duplicate_candidates
    await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=world.group_id)
    return {
        'n_history': n_history,
        'seed': seed,
        'live_shown': world.live_edge.fact in shown,
        'live_retired': any(e.uuid == world.live_edge.uuid for e in invalidated),
        'shown': len(shown),
        'expired_shown': sum(f in {h.fact for h in world.history} for f in shown),
    }


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('--depths', type=int, nargs='+', default=[0, 5, 9, 10, 12, 20, 50])
    ap.add_argument('--seeds', type=int, default=30)
    ap.add_argument('--out', default=None)
    ap.add_argument('--fix', choices=['none', '1', '2', 'both'], default='none')
    args = ap.parse_args()
    args.out = args.out or f'results/history_depth_fix-{args.fix}.jsonl'

    from graphiti_temporal import fix

    {'1': fix.apply_invalidation_filter, '2': fix.apply_fulltext_filter_order,
     'both': fix.apply, 'none': lambda: None}[args.fix]()  # fmt: skip

    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    await driver.build_indices_and_constraints()
    rows = []
    t0 = time.time()
    for n in args.depths:
        for s in range(args.seeds):
            rows.append(await trial(driver, n, s))
        sub = [r for r in rows if r['n_history'] == n]
        k = sum(r['live_retired'] for r in sub)
        lo, hi = wilson(k, len(sub))
        print(
            f'history={n:>3}  current home retired {k:>3}/{len(sub)}  '
            f'[{lo:.2f}, {hi:.2f}]  shown to model {sum(r["live_shown"] for r in sub)}/{len(sub)}  '
            f'mean expired facts in pool {sum(r["expired_shown"] for r in sub) / len(sub):.1f}',
            flush=True,
        )
    await driver.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    meta = {'fix': args.fix, 'graphiti_core': '4f98b7ca45fef96925dc04134c42c526df73bb28', 'neo4j': '5.26.2',
            'argv': sys.argv[1:], 'seconds': round(time.time() - t0, 1)}  # fmt: skip
    with open(args.out, 'w') as f:
        f.write(json.dumps({'meta': meta}) + '\n')
        f.writelines(json.dumps(r) + '\n' for r in rows)


if __name__ == '__main__':
    asyncio.run(main())
