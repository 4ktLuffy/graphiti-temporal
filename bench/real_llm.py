"""The history-depth fault through Graphiti's full `add_episode`, with a real model (Codex).

Alice's past homes are stored as Graphiti stores them; then one real episode, "Alice moved to
Berlin.", goes through extraction, entity resolution and edge resolution with Codex as the LLM.
Records whether her current home was retired, and how many homes are still open afterwards.

    uv run python bench/real_llm.py --n-history 20 --trials 15 [--fix]
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

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodeType

from graphiti_temporal import world as W
from graphiti_temporal.codex_llm import CodexLLMClient, CodexQuotaExhausted
from graphiti_temporal.stats import wilson
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, OracleLLM


async def open_homes(driver, group_id: str, alice_uuid: str) -> list[str]:
    records, _, _ = await driver.execute_query(
        """
        MATCH (a:Entity {uuid: $alice})-[e:RELATES_TO {group_id: $g}]->(m:Entity)
        WHERE e.invalid_at IS NULL AND e.expired_at IS NULL
        RETURN e.fact AS fact
        """,
        alice=alice_uuid,
        g=group_id,
    )
    return [r['fact'] for r in records]


async def trial(driver, make_llm, n_history: int, seed: int) -> dict:
    llm = make_llm()
    world = await W.build(driver, OracleLLM(), n_history=n_history, seed=seed)
    graphiti = Graphiti(
        graph_driver=driver, llm_client=llm, embedder=HashEmbedder(),
        cross_encoder=NullCrossEncoder(),
    )  # fmt: skip
    error = None
    try:
        await graphiti.add_episode(
            name='move',
            episode_body='Alice moved to Berlin.',
            source_description='chat',
            reference_time=world.new_valid_at,
            source=EpisodeType.text,
            group_id=world.group_id,
        )
    except CodexQuotaExhausted:
        raise  # stops the whole run: these are not results
    except Exception as e:  # recorded, never dropped
        error = f'{type(e).__name__}: {e}'[:300]
    live = await EntityEdge.get_by_uuid(driver, world.live_edge.uuid)
    still_open = await open_homes(driver, world.group_id, world.subject.uuid)
    await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=world.group_id)
    calls = llm.calls
    resolve = [c for c in calls if c['response_model'] == 'EdgeDuplicate']
    live_fact = world.live_edge.fact
    return {
        'n_history': n_history,
        'seed': seed,
        'live_retired': live.invalid_at is not None,
        'open_after': still_open,
        'error': error,
        'llm_calls': len(calls),
        # Was the live fact in any edge-resolution prompt, and what did the model answer?
        'live_shown': any(live_fact in c['prompt'] for c in resolve),
        'resolve_replies': [c['reply'] for c in resolve],
    }


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('--n-history', type=int, default=20)
    ap.add_argument('--trials', type=int, default=15)
    ap.add_argument('--first-seed', type=int, default=1000)
    ap.add_argument('--parallel', type=int, default=3)
    ap.add_argument('--model', default='gpt-5.6-luna')
    ap.add_argument('--effort', default='low')
    ap.add_argument('--fix', choices=['none', '1', '2', 'both'], default='none')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    from graphiti_temporal import fix

    {'1': fix.apply_invalidation_filter, '2': fix.apply_fulltext_filter_order,
     'both': fix.apply, 'none': lambda: None}[args.fix]()  # fmt: skip

    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    await driver.build_indices_and_constraints()
    codex_slots = asyncio.Semaphore(args.parallel * 2)

    def make_llm() -> CodexLLMClient:
        return CodexLLMClient(args.model, args.effort, slots=codex_slots)

    llm = make_llm()  # for its name
    slots = asyncio.Semaphore(args.parallel)
    t0 = time.time()

    async def one(seed: int) -> dict:
        async with slots:
            r = await trial(driver, make_llm, args.n_history, seed)
            print(json.dumps(r), flush=True)
            return r

    seeds = range(args.first_seed, args.first_seed + args.trials)
    try:
        rows = await asyncio.gather(*(one(s) for s in seeds))
    except CodexQuotaExhausted as e:
        print(f'\n*** CODEX CREDITS EXHAUSTED - reset the account and rerun. {e}', flush=True)
        await driver.close()
        sys.exit(3)
    await driver.close()
    ok = [r for r in rows if r['error'] is None]
    k = sum(r['live_retired'] for r in ok)
    lo, hi = wilson(k, len(ok))
    print(
        f'{llm.name} fix={args.fix} history={args.n_history}: current home retired {k}/{len(ok)} '
        f'[{lo:.2f}, {hi:.2f}], errors {len(rows) - len(ok)}, '
        f'live fact shown {sum(r["live_shown"] for r in ok)}/{len(ok)}, {time.time() - t0:.0f}s'
    )
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    meta = {'fix': args.fix, 'graphiti_core': '4f98b7ca45fef96925dc04134c42c526df73bb28', 'neo4j': '5.26.2',
            'llm': llm.name, 'argv': sys.argv[1:],
            'seconds': round(time.time() - t0, 1)}  # fmt: skip
    with open(args.out, 'w') as f:
        f.write(json.dumps({'meta': meta}) + '\n')
        f.writelines(json.dumps(r) + '\n' for r in rows)


if __name__ == '__main__':
    asyncio.run(main())
