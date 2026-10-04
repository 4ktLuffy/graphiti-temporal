"""Does `graphiti-gate audit` find exactly the damage that is there, and nothing that is not?

Builds graphs whose damage is known: a person with N past homes moves once through Graphiti's real
`resolve_extracted_edges` (with or without fix 1), and the result is saved as `add_episode` would
save it. Ground truth is whether the current home was retired. One graph also gets a planted
inverted interval and a planted ended-but-not-expired fact. The audit must flag exactly the graphs
left with two current homes, and the planted facts.

    uv run python bench/validate_audit.py [--fix] --graphs 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.tracer import NoOpTracer
from graphiti_core.utils.maintenance import edge_operations

from graphiti_gate.audit import audit
from graphiti_temporal import world as W
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, OracleLLM, hash_embedding

UTC = timezone.utc


async def damaged_graph(driver, n_history: int, seed: int) -> tuple[str, bool]:
    oracle = OracleLLM()
    clients = GraphitiClients(
        driver=driver, llm_client=oracle, embedder=HashEmbedder(),
        cross_encoder=NullCrossEncoder(), tracer=NoOpTracer(),
    )  # fmt: skip
    world = await W.build(driver, oracle, n_history=n_history, seed=seed)
    edge, episode, entities = await W.new_move(driver, oracle, world)
    resolved, invalidated, _ = await edge_operations.resolve_extracted_edges(
        clients, [edge], episode, entities, {}, {}
    )
    for e in [*resolved, *invalidated]:
        e.fact_embedding = e.fact_embedding or hash_embedding(e.fact)
        await e.save(driver)
    retired = any(e.uuid == world.live_edge.uuid for e in invalidated)
    return world.group_id, not retired


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('--fix', action='store_true')
    ap.add_argument('--graphs', type=int, default=20)
    args = ap.parse_args()
    if args.fix:
        from graphiti_temporal import fix

        fix.apply_invalidation_filter()
    driver = Neo4jDriver('bolt://localhost:7687', 'neo4j', os.environ['NEO4J_PASSWORD'])
    await driver.build_indices_and_constraints()
    truth: dict[str, bool] = {}
    for seed in range(args.graphs):
        group, damaged = await damaged_graph(driver, 20, 5000 + seed)
        truth[group] = damaged

    planted_group = next(iter(truth))
    alice, city = '', ''
    records, _, _ = await driver.execute_query(
        "MATCH (a:Entity {group_id: $g, name: 'Alice'}), (c:Entity {group_id: $g, name: 'Berlin'}) "
        'RETURN a.uuid AS a, c.uuid AS c',
        g=planted_group,
    )
    alice, city = records[0]['a'], records[0]['c']
    now = datetime.now(UTC)
    for fact, valid, invalid, expired in [
        ('Alice visited Berlin backwards', datetime(2010, 6, 1, tzinfo=UTC), datetime(2009, 1, 1, tzinfo=UTC), now),
        ('Alice studied in Berlin', datetime(2001, 1, 1, tzinfo=UTC), datetime(2002, 1, 1, tzinfo=UTC), None),
    ]:  # fmt: skip
        await EntityEdge(
            source_node_uuid=alice, target_node_uuid=city, group_id=planted_group, name='VISITED',
            fact=fact, fact_embedding=hash_embedding(fact), episodes=[], created_at=now,
            valid_at=valid, invalid_at=invalid, expired_at=expired,
        ).save(driver)  # fmt: skip

    result = await audit(driver, list(truth), exposure=9)
    flagged = {d['group_id'] for d in result['two_current_values'] if d['relation'] == 'LIVES_IN'}
    damaged = {g for g, d in truth.items() if d}
    print(json.dumps({
        'fix': args.fix,
        'graphs': len(truth),
        'damaged_truth': len(damaged),
        'flagged': len(flagged),
        'true_positives': len(flagged & damaged),
        'false_positives': len(flagged - damaged),
        'missed': len(damaged - flagged),
        'exposed_flagged': len({e['group_id'] for e in result['exposed']}),
        'inverted_found': [r['fact'] for r in result['inverted']],
        'ended_not_expired_found': [r['fact'] for r in result['ended_not_expired']],
        'repairs_proposed': len(result['proposed_repairs']),
    }, indent=1))  # fmt: skip
    for g in truth:
        await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=g)
    await driver.close()


if __name__ == '__main__':
    asyncio.run(main())
