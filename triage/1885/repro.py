import os
import logging

logging.disable(logging.CRITICAL)
import asyncio
import traceback
import uuid
from datetime import datetime, timezone

from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode

CASES = {
    'flat': {'city': 'X'},
    'nested_dict': {'address': {'city': 'X'}},
    'list_of_dicts': {'addrs': [{'city': 'X'}]},
    'list_of_prims': {'tags': ['a', 'b']},
}


async def run(label, driver, gid):
    emb = [0.1] * 1024
    for name, attrs in CASES.items():
        n = EntityNode(
            name=f'n-{name}', group_id=gid, summary='s', name_embedding=emb, attributes=attrs
        )
        m = EntityNode(name=f'm-{name}', group_id=gid, summary='s', name_embedding=emb)
        for kind, obj in (('node', n), ('edge', None)):
            try:
                if kind == 'node':
                    await n.save(driver)
                else:
                    n2 = EntityNode(
                        uuid=n.uuid, name=n.name, group_id=gid, summary='s', name_embedding=emb
                    )
                    await n2.save(driver)
                    await m.save(driver)
                    e = EntityEdge(
                        source_node_uuid=n.uuid,
                        target_node_uuid=m.uuid,
                        group_id=gid,
                        name='R',
                        fact='f',
                        fact_embedding=emb,
                        episodes=[],
                        created_at=datetime.now(timezone.utc),
                        attributes=attrs,
                    )
                    await e.save(driver)
                print(f'{label} {kind:4} {name:14} OK')
            except Exception as ex:
                tb = traceback.extract_tb(ex.__traceback__)
                gc = [f for f in tb if 'graphiti_core' in f.filename][-1]
                print(
                    f'{label} {kind:4} {name:14} FAIL {type(ex).__name__}: {str(ex)[:140]!r} @ {gc.filename.split("graphiti_core/")[-1]}:{gc.lineno}'
                )


async def main():
    gid = 'triage1885_' + uuid.uuid4().hex[:8]
    d = Neo4jDriver('bolt://localhost:7688', 'neo4j', os.environ['NEO4J_PASSWORD'])
    try:
        await run('neo4j', d, gid)
        await d.execute_query('MATCH (n {group_id:$g}) DETACH DELETE n', g=gid)
    finally:
        await d.close()
    for port in (6379, 6380):
        gid2 = 'triage1885_' + uuid.uuid4().hex[:8]
        f = FalkorDriver(host='localhost', port=port, database=gid2)
        try:
            await run(f'falkor{port}', f, gid2)
        finally:
            try:
                await f.client.select_graph(gid2).delete()
            except Exception as ex:
                print('cleanup', ex)
            await f.close()


asyncio.run(main())
