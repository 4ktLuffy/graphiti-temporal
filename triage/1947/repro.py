import asyncio
import datetime as dt
import sys

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.driver import GraphProvider
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.graph_queries import get_fulltext_indices, get_range_indices
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
from graphiti_core.tracer import NoOpTracer


class Emb(EmbedderClient):
    async def create(self, input_data):
        return [0.01 * (i + 1) for i in range(64)]

    async def create_batch(self, l):
        return [await self.create(x) for x in l]


class CE(CrossEncoderClient):
    async def rank(self, q, p):
        return [(x, 1.0) for x in p]


class LLM(LLMClient):
    def __init__(self):
        super().__init__(LLMConfig(api_key='stub', model='stub'))

    async def _generate_response(self, *a, **k):
        return {}


async def step(name, coro):
    try:
        r = await coro
        print(f'OK    {name}')
        return r
    except Exception as e:
        print(f'FAIL  {name}: {type(e).__name__}: {str(e)[:200]}')


async def main(port):
    d = FalkorDriver(port=port, database=f'repro{port}')
    await d.execute_query('MATCH (n) DETACH DELETE n') if False else None
    print('== port', port)
    for q in get_range_indices(GraphProvider.FALKORDB) + get_fulltext_indices(
        GraphProvider.FALKORDB
    ):
        await step(' '.join(q.split())[:70], d.execute_query(q))
    await step('build_indices_and_constraints()', d.build_indices_and_constraints())
    g = Graphiti(
        graph_driver=d, llm_client=LLM(), embedder=Emb(), cross_encoder=CE(), tracer=NoOpTracer()
    )
    now = dt.datetime.now(dt.timezone.utc)
    a = EntityNode(name='Alice', group_id='g', summary='alice person')
    b = EntityNode(name='Bob', group_id='g', summary='bob person')
    for n in (a, b):
        n.name_embedding = await Emb().create(n.name)
    await step('EntityNode.save x2', asyncio.gather(a.save(d), b.save(d)))
    e = EntityEdge(
        group_id='g',
        source_node_uuid=a.uuid,
        target_node_uuid=b.uuid,
        created_at=now,
        name='KNOWS',
        fact='Alice knows Bob',
    )
    e.fact_embedding = await Emb().create(e.fact)
    await step('EntityEdge.save', e.save(d))
    ep = EpisodicNode(
        name='ep',
        group_id='g',
        source=EpisodeType.text,
        source_description='s',
        content='Alice knows Bob',
        valid_at=now,
        entity_edges=[e.uuid],
    )
    await step('EpisodicNode.save', ep.save(d))
    r = await step('search (hybrid fulltext+vector)', g.search('Alice', group_ids=['g']))
    if r is not None:
        print('      edge results:', len(r))
    r = await step(
        'search_ (combined hybrid, nodes/edges/episodes)',
        g.search_('Alice', config=COMBINED_HYBRID_SEARCH_RRF, group_ids=['g']),
    )
    if r is not None:
        print('      nodes', len(r.nodes), 'edges', len(r.edges), 'episodes', len(r.episodes))
    await step(
        'add_triplet',
        g.add_triplet(
            EntityNode(name='Carol', group_id='g'),
            EntityEdge(
                group_id='g',
                source_node_uuid='x',
                target_node_uuid='y',
                created_at=now,
                name='LIKES',
                fact='Carol likes Dave',
            ),
            EntityNode(name='Dave', group_id='g'),
        ),
    )
    await d.close()


asyncio.run(main(int(sys.argv[1])))
