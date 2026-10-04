"""What does returning every fulltext hit cost, compared with limiting inside the index?

Builds one group whose facts all match the query, M of them, 1 in 10 still valid, and times
Graphiti's filtered fulltext query in both forms on Neo4j. Median of repeated runs, warm cache.

    uv run python bench/fulltext_cost.py --sizes 1000 10000 50000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import statistics
import time
import uuid
from pathlib import Path

from graphiti_core.driver.neo4j_driver import Neo4jDriver

QUERY_TAIL = """
YIELD relationship AS rel, score
MATCH (n:Entity)-[e:RELATES_TO {uuid: rel.uuid}]->(m:Entity)
WHERE (e.invalid_at IS NULL) AND e.group_id IN $group_ids
WITH e, score, n, m
RETURN e.uuid AS uuid, e.fact AS fact
ORDER BY score DESC
LIMIT $limit
"""
FORMS = {
    'index limit (current)': 'CALL db.index.fulltext.queryRelationships("edge_name_and_fact", $query, {limit: $limit})',
    'no index limit (patch 2)': 'CALL db.index.fulltext.queryRelationships("edge_name_and_fact", $query)',
}  # fmt: skip


async def build(driver, group_id: str, m: int) -> None:
    rows = [{'i': i, 'u': str(uuid.uuid4()), 'open': i % 10 == 0} for i in range(m)]
    for start in range(0, m, 5000):
        await driver.execute_query(
            """
            UNWIND $rows AS r
            CREATE (a:Entity {uuid: r.u + '-a', group_id: $g, name: 'Person ' + r.i})
            CREATE (b:Entity {uuid: r.u + '-b', group_id: $g, name: 'City ' + r.i})
            CREATE (a)-[:RELATES_TO {uuid: r.u, group_id: $g, name: 'LIVES_IN',
                fact: 'Alice lives in city ' + r.i,
                invalid_at: CASE WHEN r.open THEN null ELSE datetime('2000-01-01T00:00:00Z') END}]->(b)
            """,
            rows=rows[start : start + 5000],
            g=group_id,
        )
    await driver.execute_query('CALL db.awaitIndexes(300)')


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('--sizes', type=int, nargs='+', default=[1000, 10000, 50000])
    ap.add_argument('--repeats', type=int, default=15)
    ap.add_argument('--out', default='results/fulltext_cost.jsonl')
    args = ap.parse_args()
    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    await driver.build_indices_and_constraints()
    rows = []
    for m in args.sizes:
        g = f'cost-{m}-{uuid.uuid4().hex[:8]}'
        await build(driver, g, m)
        lucene = f'group_id:"{g}" AND (Alice lives)'
        for name, call in FORMS.items():
            times, found = [], 0
            for i in range(args.repeats + 2):
                t = time.perf_counter()
                recs, _, _ = await driver.execute_query(
                    call + QUERY_TAIL, query=lucene, limit=20, group_ids=[g], routing_='r'
                )
                if i >= 2:  # first two warm the cache
                    times.append((time.perf_counter() - t) * 1000)
                found = len(recs)
            row = {'facts': m, 'form': name, 'median_ms': round(statistics.median(times), 1),
                   'p90_ms': round(sorted(times)[int(0.9 * len(times)) - 1], 1),
                   'rows_returned': found}  # fmt: skip
            rows.append(row)
            print(json.dumps(row), flush=True)
        while True:
            recs, _, _ = await driver.execute_query(
                'MATCH (n:Entity {group_id: $g}) WITH n LIMIT 5000 DETACH DELETE n RETURN count(*) AS c',
                g=g,
            )
            if recs[0]['c'] == 0:
                break
    await driver.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, 'w') as f:
        f.write(json.dumps({'meta': {'neo4j': '5.26.2', 'repeats': args.repeats}}) + '\n')
        f.writelines(json.dumps(r) + '\n' for r in rows)


if __name__ == '__main__':
    asyncio.run(main())
