"""Does an "as of T" search find the fact that was true at T, as a subject's history grows?

Uses Graphiti's public `search()` with the date filter its maintainers recommend for this
(getzep/graphiti#769): valid_at <= T AND (invalid_at IS NULL OR invalid_at > T). Compares the
default hybrid search with each of its two channels alone.

    uv run python bench/as_of.py --depths 0 10 20 50 --seeds 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.search import search as search_module
from graphiti_core.search.search_config import (
    EdgeReranker,
    EdgeSearchConfig,
    EdgeSearchMethod,
    SearchConfig,
)
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from graphiti_core.tracer import NoOpTracer

from graphiti_temporal import world as W
from graphiti_temporal.stats import wilson
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, OracleLLM

UTC = timezone.utc
CHANNELS = {
    'hybrid (default)': [EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity],
    'fulltext only': [EdgeSearchMethod.bm25],
    'vector only': [EdgeSearchMethod.cosine_similarity],
}


def as_of(t: datetime) -> SearchFilters:
    lte, gt = ComparisonOperator.less_than_equal, ComparisonOperator.greater_than
    return SearchFilters(
        valid_at=[[DateFilter(date=t, comparison_operator=lte)]],
        invalid_at=[
            [DateFilter(comparison_operator=ComparisonOperator.is_null)],
            [DateFilter(date=t, comparison_operator=gt)],
        ],
    )


def truth(world: W.World, t: datetime) -> str:
    for e in [world.live_edge, *world.history]:
        if e.valid_at <= t and (e.invalid_at is None or e.invalid_at > t):
            return e.fact
    raise AssertionError(t)


async def trial(driver, n_history: int, seed: int) -> list[dict]:
    oracle = OracleLLM()
    clients = GraphitiClients(
        driver=driver, llm_client=oracle, embedder=HashEmbedder(),
        cross_encoder=NullCrossEncoder(), tracer=NoOpTracer(),
    )  # fmt: skip
    world = await W.build(driver, oracle, n_history=n_history, seed=seed)
    rng = random.Random(seed)
    times = {'now': datetime(2200, 1, 1, tzinfo=UTC)}
    if world.history:
        h = rng.choice(world.history)
        times['past'] = datetime(h.valid_at.year, 7, 1, tzinfo=UTC)
    rows = []
    for when, t in times.items():
        want = truth(world, t)
        for channel, methods in CHANNELS.items():
            config = SearchConfig(
                edge_config=EdgeSearchConfig(search_methods=methods, reranker=EdgeReranker.rrf)
            )
            res = await search_module.search(
                clients, 'Alice lives in', [world.group_id], config, as_of(t)
            )
            rows.append({
                'n_history': n_history, 'seed': seed, 'when': when, 'channel': channel,
                'found': want in [e.fact for e in res.edges], 'returned': len(res.edges),
            })  # fmt: skip
    await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=world.group_id)
    return rows


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('--depths', type=int, nargs='+', default=[0, 10, 20, 50])
    ap.add_argument('--seeds', type=int, default=20)
    ap.add_argument('--out', default=None)
    ap.add_argument('--fix', choices=['none', '1', '2', 'both'], default='none')
    args = ap.parse_args()
    args.out = args.out or f'results/as_of_fix-{args.fix}.jsonl'
    from graphiti_temporal import fix

    {'1': fix.apply_invalidation_filter, '2': fix.apply_fulltext_filter_order,
     'both': fix.apply, 'none': lambda: None}[args.fix]()  # fmt: skip

    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    await driver.build_indices_and_constraints()
    rows: list[dict] = []
    t0 = time.time()
    for n in args.depths:
        for s in range(args.seeds):
            rows += await trial(driver, n, s)
        for when in ('now', 'past'):
            for channel in CHANNELS:
                sub = [r for r in rows if (r['n_history'], r['when'], r['channel']) == (n, when, channel)]  # fmt: skip
                if not sub:
                    continue
                k = sum(r['found'] for r in sub)
                lo, hi = wilson(k, len(sub))
                print(f'history={n:>3} as-of {when:<4} {channel:<17} found {k:>3}/{len(sub)} [{lo:.2f}, {hi:.2f}]', flush=True)  # fmt: skip
    await driver.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    meta = {'fix': args.fix, 'graphiti_core': '4f98b7ca45fef96925dc04134c42c526df73bb28', 'neo4j': '5.26.2',
            'argv': sys.argv[1:], 'seconds': round(time.time() - t0, 1)}  # fmt: skip
    with open(args.out, 'w') as f:
        f.write(json.dumps({'meta': meta}) + '\n')
        f.writelines(json.dumps(r) + '\n' for r in rows)


if __name__ == '__main__':
    asyncio.run(main())
