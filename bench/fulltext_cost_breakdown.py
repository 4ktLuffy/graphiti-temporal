"""Where the time goes in an unlimited fulltext query: the index, Graphiti's per-hit re-MATCH,
or the filter. Backs the breakdown table in DESIGN.md ("Fix 2 and its cost").

    uv run python bench/fulltext_cost_breakdown.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import statistics
import sys
import time
import uuid
from pathlib import Path

from graphiti_core.driver.neo4j_driver import Neo4jDriver

sys.path.insert(0, str(Path(__file__).parent))
from fulltext_cost import build  # noqa: E402

CALL = (
    'CALL db.index.fulltext.queryRelationships("edge_name_and_fact", $query{opt}) '
    'YIELD relationship AS rel, score '
)
REMATCH = 'MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity) '
QUERIES = {
    'raw hits only, no limit': CALL.format(opt='') + 'RETURN count(*) AS c',
    'hits + re-MATCH by uuid, no limit': CALL.format(opt='') + REMATCH + 'RETURN count(*) AS c',
    'hits used directly + filter, no limit': CALL.format(opt='')
    + 'WITH rel AS e, score WHERE e.invalid_at IS NULL '
    'RETURN e.uuid ORDER BY score DESC LIMIT 20',
    'over-fetch 200 + re-MATCH + filter': CALL.format(opt=', {limit: 200}')
    + REMATCH
    + 'WHERE e.invalid_at IS NULL RETURN e.uuid ORDER BY score DESC LIMIT 20',
}


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    await driver.build_indices_and_constraints()
    for size in (1000, 10000, 50000):
        group = f'cb-{size}-{uuid.uuid4().hex[:6]}'
        await build(driver, group, size)
        lucene = f'group_id:"{group}" AND (Alice lives)'
        for name, cypher in QUERIES.items():
            times = []
            for _ in range(8):
                start = time.perf_counter()
                records, _, _ = await driver.execute_query(cypher, query=lucene, routing_='r')
                times.append((time.perf_counter() - start) * 1000)
            rows = records[0][0] if len(records) == 1 else len(records)
            median = statistics.median(times[2:])  # first two warm the cache
            print(f'{size:>6} {name:<40} {median:8.1f} ms  rows {rows}')
        while True:
            records, _, _ = await driver.execute_query(
                'MATCH (n:Entity {group_id: $g}) WITH n LIMIT 5000 DETACH DELETE n '
                'RETURN count(*) AS c',
                g=group,
            )
            if records[0]['c'] == 0:
                break
    await driver.close()


if __name__ == '__main__':
    asyncio.run(main())
