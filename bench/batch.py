"""Two back-filled homes in one episode: does batching change what Graphiti stores?

`add_episode` resolves all of an episode's edges in one `resolve_extracted_edges` call, and edges
of the same batch do not see each other. Here one person has a crowded history
(`graphiti_gate.fuzz.crowded`, without its back-filled home), then two old homes arrive, either
in one call (one episode) or one after the other. The two are adjacent in time, so the earlier
must end where the later begins. The correct end of each is the start of the next home by date,
counting the other new one. Oracle judge, Neo4j, Graphiti main with the
runtime fixes in `--fixes`.

    NEO4J_PASSWORD=... python bench/batch.py --fixes none|1|1+3 --out results/batch-<fixes>.jsonl
"""

import argparse
import asyncio
import json
import logging
import random
import uuid
from datetime import datetime, timezone

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.utils.maintenance import edge_operations

from graphiti_gate.compat import make_clients
from graphiti_gate.fuzz import close_backend, crowded, open_backend, snapshot
from graphiti_gate.judges import Claim, Judge
from graphiti_temporal import fix
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, hash_embedding
from graphiti_temporal.world import CITIES

UTC = timezone.utc


async def ingest(seed: int, batched: bool) -> dict:
    ops = crowded(seed)[:-1]  # the crowded history without its own back-filled home
    rng = random.Random(f'batch-{seed}')
    taken = {o.year for o in ops}
    # Two adjacent old homes: no existing home starts between them, so the first must end exactly
    # where its sibling begins, which in one batch it cannot see.
    while True:
        y1 = rng.choice([y for y in range(1951, 2023) if y not in taken])
        nxt = min([y for y in taken if y > y1], default=2025)
        if nxt - y1 >= 2:
            late_years = [y1, rng.randrange(y1 + 1, nxt)]
            break
    late_cities = rng.sample([c for c in CITIES if c not in {o.value for o in ops}], 2)
    group = f'bt{uuid.uuid4().hex[:10]}'
    driver = await open_backend('neo4j', group)
    claims = {o.fact: Claim('Alice', 'home', o.value) for o in ops}
    claims |= {f'Alice lives in {c}': Claim('Alice', 'home', c) for c in late_cities}
    clients = make_clients(driver=driver, llm_client=Judge(persona='oracle', claims=claims),
                           embedder=HashEmbedder(), cross_encoder=NullCrossEncoder())  # fmt: skip
    nodes, now = {}, datetime.now(UTC)

    async def node(name):
        if name not in nodes:
            nodes[name] = EntityNode(
                name=name, group_id=group, labels=['Entity'], name_embedding=hash_embedding(name)
            )
            await nodes[name].save(driver)
        return nodes[name]

    def edge(city, fact, year):
        return EntityEdge(source_node_uuid=nodes['Alice'].uuid, target_node_uuid=nodes[city].uuid, group_id=group,
                          name='LIVES_IN', fact=fact, fact_embedding=hash_embedding(fact), episodes=[],
                          created_at=now, valid_at=datetime(year, 1, 1, tzinfo=UTC))  # fmt: skip

    async def resolve(edges, year):
        episode = EpisodicNode(name='e', group_id=group, source=EpisodeType.text, source_description='b',
                               content=' '.join(e.fact for e in edges), valid_at=datetime(year, 1, 1, tzinfo=UTC))  # fmt: skip
        resolved, invalidated, *_ = await edge_operations.resolve_extracted_edges(
            clients, edges, episode, list(nodes.values()), {}, {}
        )
        for e in [*resolved, *invalidated]:
            e.fact_embedding = e.fact_embedding or hash_embedding(e.fact)
            await e.save(driver)

    try:
        for name in ['Alice', *{o.value for o in ops}, *late_cities]:
            await node(name)
        for o in ops:
            await resolve([edge(o.value, o.fact, o.year)], o.year)
        late = [
            edge(c, f'Alice lives in {c}', y) for c, y in zip(late_cities, late_years, strict=True)
        ]
        if batched:
            await resolve(late, max(late_years))
        else:
            for e, y in zip(late, late_years, strict=True):
                await resolve([e], y)
        state = await snapshot(driver, group)
    finally:
        await close_backend('neo4j', driver, group)
    years = sorted([*(o.year for o in ops), *late_years])
    want = {f'Alice lives in {c}': f'{years[years.index(y) + 1]}-01-01' if y != years[-1] else None
            for c, y in zip(late_cities, late_years, strict=True)}  # fmt: skip
    got = {t[2]: t[4] for t in state if t[2] in want}
    return {'seed': seed, 'batched': batched, 'want': want, 'got': got, 'correct': got == want}


async def main() -> None:
    logging.disable(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument('--fixes', choices=['none', '1', '1+3'], default='none')
    ap.add_argument('--seeds', type=int, default=20)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    if args.fixes in ('1', '1+3'):
        fix.apply_invalidation_filter()
    if args.fixes == '1+3':
        fix.apply_backfill_neighbors()
    rows = []
    with open(args.out, 'w') as f:
        for seed in range(args.seeds):
            for batched in (False, True):
                rows.append(await ingest(seed, batched))
                f.write(json.dumps(rows[-1]) + '\n')
    for batched in (False, True):
        sub = [r for r in rows if r['batched'] == batched]
        print(
            f'fixes={args.fixes} {"one episode" if batched else "one at a time"}: '
            f'both back-filled homes end correctly in {sum(r["correct"] for r in sub)}/{len(sub)}'
        )


if __name__ == '__main__':
    asyncio.run(main())
