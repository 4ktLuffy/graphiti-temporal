"""What the temporal-neighbor lookup (upstream/03) costs as a subject's history grows.

Times the patch's own `temporal_neighbors` on Neo4j and FalkorDB, for a subject with 10, 100 and
1,000 facts, back-filled in the middle of its history, against reading every fact of the subject
(`EntityEdge.get_by_node_uuid`), the first design. Run it inside a revision that has the patch,
with nothing else using the databases:

    python -c "from graphiti_gate.revisions import prepare; print(prepare('patch:bench/patches/01+03-neighbors.patch').python)"
    NEO4J_PASSWORD=... FALKORDB_PORT=6380 <that python> bench/neighbors_cost.py
"""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone

from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.maintenance.edge_operations import temporal_neighbors

from graphiti_temporal.testing import hash_embedding

UTC = timezone.utc


async def history(driver, group: str, n: int) -> EntityNode:
    alice = EntityNode(
        name='Alice', group_id=group, labels=['Entity'], name_embedding=hash_embedding('Alice')
    )
    await alice.save(driver)
    now = datetime.now(UTC)
    for i in range(n):
        city = EntityNode(name=f'City{i}', group_id=group, labels=['Entity'],
                          name_embedding=hash_embedding(f'City{i}'))  # fmt: skip
        await city.save(driver)
        fact = f'Alice lives in City{i}'
        end = datetime(1001 + i, 1, 1, tzinfo=UTC) if i < n - 1 else None
        await EntityEdge(source_node_uuid=alice.uuid, target_node_uuid=city.uuid, group_id=group,
                         name='LIVES_IN', fact=fact, fact_embedding=hash_embedding(fact), episodes=[],
                         created_at=now, valid_at=datetime(1000 + i, 1, 1, tzinfo=UTC),
                         invalid_at=end, expired_at=now if end else None).save(driver)  # fmt: skip
    return alice


async def time_it(fn, runs: int = 30) -> tuple[float, float]:
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        await fn()
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    return round(times[runs // 2], 1), round(times[int(runs * 0.95) - 1], 1)


async def main() -> None:
    backends = {
        'neo4j': lambda g: Neo4jDriver(os.environ.get('NEO4J_URI', 'bolt://localhost:7688'),
                                       os.environ.get('NEO4J_USER', 'neo4j'), os.environ['NEO4J_PASSWORD']),
        'falkordb': lambda g: FalkorDriver(host='localhost', port=int(os.environ.get('FALKORDB_PORT', 6380)),
                                           database=g),
    }  # fmt: skip
    for backend, make in backends.items():
        for n in (10, 100, 1000):
            group = f'cost-{uuid.uuid4().hex[:10]}'
            driver = make(group)
            await driver.build_indices_and_constraints()
            alice = await history(driver, group, n)
            new = EntityEdge(source_node_uuid=alice.uuid, target_node_uuid=alice.uuid, group_id=group,
                             name='LIVES_IN', fact='Alice lives in Oslo', episodes=[],
                             created_at=datetime.now(UTC),
                             valid_at=datetime(1000 + n // 2, 6, 1, tzinfo=UTC))  # fmt: skip
            found = await temporal_neighbors(driver, new, [])
            p50, p95 = await time_it(lambda d=driver, e=new: temporal_neighbors(d, e, []))
            a50, a95 = await time_it(
                lambda d=driver, u=alice.uuid: EntityEdge.get_by_node_uuid(d, u)
            )
            print(json.dumps({'backend': backend, 'facts': n, 'returned': len(found),
                              'neighbors_p50_ms': p50, 'neighbors_p95_ms': p95,
                              'read_all_p50_ms': a50, 'read_all_p95_ms': a95}), flush=True)  # fmt: skip
            await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
            await driver.close()


if __name__ == '__main__':
    asyncio.run(main())
