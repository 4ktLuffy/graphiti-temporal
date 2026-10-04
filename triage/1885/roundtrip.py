import asyncio
import os
import uuid

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.nodes import EntityNode


async def main():
    d = Neo4jDriver('bolt://localhost:7688', 'neo4j', os.environ['NEO4J_PASSWORD'])
    g = f'rt-{uuid.uuid4().hex[:8]}'
    n = EntityNode(
        name='Alice',
        group_id=g,
        labels=['Entity'],
        name_embedding=[0.1] * 16,
        attributes={'address': {'city': 'Paris'}, 'tags': ['a', 'b']},
    )
    try:
        await n.save(d)
        back = await EntityNode.get_by_uuid(d, n.uuid)
        print(
            'saved ok; read back address =',
            repr(back.attributes.get('address')),
            type(back.attributes.get('address')).__name__,
        )
    except Exception as e:
        print('save/read failed:', type(e).__name__, str(e)[:120])
    await d.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=g)
    await d.close()


asyncio.run(main())
