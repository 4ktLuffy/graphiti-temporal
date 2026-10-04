"""What does fix 1 cost, and where does it stop helping? Neo4j only, no model.

1. Latency: the invalidation-candidate search Graphiti runs for a new fact (hybrid, limit 10),
   with no filter (main) and with fix 1's filter, as a person's superseded history grows.
   Median and p95 over warm repeats.
2. Prompt size: the characters of candidate facts the model is shown, both ways.
3. The limit fix 1 does not lift: with H superseded homes and K *still-valid* facts about the same
   person (job, hobbies...), is the current home among the 10 candidates?

    NEO4J_PASSWORD=... uv run python bench/fix_cost.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import statistics
import time
from datetime import datetime, timedelta, timezone

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from graphiti_core.search import search as search_module
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import SearchFilters

from graphiti_gate.compat import make_clients
from graphiti_temporal.fix import invalidation_candidate_filter
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, OracleLLM, hash_embedding
from graphiti_temporal.world import CITIES

UTC = timezone.utc
HOBBIES = ['plays chess', 'runs marathons', 'paints watercolours', 'keeps bees', 'sails', 'climbs',
           'bakes bread', 'collects stamps', 'plays cello', 'grows tomatoes', 'writes poetry',
           'restores bikes', 'brews coffee', 'studies Latin', 'flies drones', 'knits', 'surfs',
           'builds radios', 'plays go', 'birdwatches', 'fences', 'juggles', 'rows', 'dances salsa',
           'carves wood', 'plays darts', 'skis', 'hikes', 'fishes', 'boxes']  # fmt: skip


async def build(driver, group: str, history: int, still_valid: int) -> tuple[EntityEdge, list[str]]:
    now = datetime.now(UTC)
    alice = EntityNode(name='Alice', group_id=group, labels=['Entity'], name_embedding=hash_embedding('Alice'))  # fmt: skip
    await alice.save(driver)

    async def fact(target: str, text: str, start: datetime, end: datetime | None = None):
        node = EntityNode(name=target, group_id=group, labels=['Entity'], name_embedding=hash_embedding(target))  # fmt: skip
        await node.save(driver)
        edge = EntityEdge(
            source_node_uuid=alice.uuid, target_node_uuid=node.uuid, group_id=group, name='R',
            fact=text, fact_embedding=hash_embedding(text), episodes=[], created_at=now,
            valid_at=start, invalid_at=end, expired_at=now if end else None,
        )  # fmt: skip
        await edge.save(driver)
        return edge

    for i in range(history):  # monthly moves from 1900: even 1,000 homes end before 1990
        city = CITIES[i] if i < len(CITIES) else f'Town{i}'
        start = datetime(1900, 1, 1, tzinfo=UTC) + timedelta(days=30 * i)
        await fact(city, f'Alice lives in {city}', start, start + timedelta(days=30))
    current = await fact('Tokyo', 'Alice lives in Tokyo', datetime(2020, 1, 1, tzinfo=UTC))
    for i in range(still_valid):
        await fact(f'hobby{i}', f'Alice {HOBBIES[i % len(HOBBIES)]} in Tokyo', datetime(2021, 1, 1, tzinfo=UTC))  # fmt: skip
    return current, []


async def candidates(clients, group: str, new: EntityEdge, search_filter: SearchFilters):
    t = time.perf_counter()
    result = await search_module.search(
        clients, new.fact, [group], EDGE_HYBRID_SEARCH_RRF, search_filter
    )
    return (time.perf_counter() - t) * 1000, result.edges


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    driver = Neo4jDriver('bolt://localhost:7687', 'neo4j', os.environ['NEO4J_PASSWORD'])
    clients = make_clients(driver=driver, llm_client=OracleLLM(), embedder=HashEmbedder(),
                           cross_encoder=NullCrossEncoder())  # fmt: skip
    await driver.build_indices_and_constraints()
    rows = []
    # 1 + 2: latency and prompt size as history grows (no other facts).
    for history in (0, 10, 50, 200, 1000):
        group = f'cost-{history}-{random.randrange(10**9)}'
        await build(driver, group, history, 0)
        new = EntityEdge(source_node_uuid='x', target_node_uuid='y', group_id=group, name='R',
                         fact='Alice lives in Berlin', episodes=[], created_at=datetime.now(UTC),
                         valid_at=datetime(2024, 6, 1, tzinfo=UTC))  # fmt: skip
        for arm, flt in (
            ('main', SearchFilters()),
            ('fix 1', invalidation_candidate_filter(new, [])),
        ):
            times, edges = [], []
            for i in range(22):
                ms, edges = await candidates(clients, group, new, flt)
                if i >= 2:
                    times.append(ms)
            times.sort()
            row = {'part': 'cost', 'history': history, 'arm': arm,
                   'median_ms': round(statistics.median(times), 1), 'p95_ms': round(times[int(0.95 * len(times)) - 1], 1),
                   'candidates': len(edges), 'prompt_chars': sum(len(e.fact) for e in edges),
                   'current_shown': any(e.fact == 'Alice lives in Tokyo' for e in edges)}  # fmt: skip
            rows.append(row)
            print(json.dumps(row), flush=True)
        await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
    # 3: still-valid facts about the same person, with 20 superseded homes, fix 1 on.
    for still_valid in (0, 5, 9, 10, 15, 30):
        shown = 0
        for seed in range(10):
            group = f'lim-{still_valid}-{seed}-{random.randrange(10**9)}'
            await build(driver, group, 20, still_valid)
            new = EntityEdge(source_node_uuid='x', target_node_uuid='y', group_id=group, name='R',
                             fact='Alice lives in Berlin', episodes=[], created_at=datetime.now(UTC),
                             valid_at=datetime(2024, 6, 1, tzinfo=UTC))  # fmt: skip
            _, edges = await candidates(clients, group, new, invalidation_candidate_filter(new, []))
            shown += any(e.fact == 'Alice lives in Tokyo' for e in edges)
            await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
        row = {'part': 'limit', 'still_valid_facts': still_valid, 'current_shown': f'{shown}/10'}
        rows.append(row)
        print(json.dumps(row), flush=True)
    await driver.close()
    with open('results/fix_cost.jsonl', 'w') as f:
        f.writelines(json.dumps(r) + '\n' for r in rows)


if __name__ == '__main__':
    asyncio.run(main())
