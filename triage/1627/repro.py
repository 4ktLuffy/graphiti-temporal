import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timezone

from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EpisodicEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_config_recipes import (
    COMBINED_HYBRID_SEARCH_RRF,
    EDGE_HYBRID_SEARCH_RRF,
    NODE_HYBRID_SEARCH_RRF,
)
from graphiti_core.search.search_utils import get_episodes_by_mentions
from graphiti_core.tracer import NoOpTracer

DIM = 1024


class Emb(EmbedderClient):
    async def create(self, input_data):
        t = input_data if isinstance(input_data, str) else str(input_data)
        h = hashlib.sha256(t.encode()).digest()
        return [(h[i % 32] - 127) / 127 for i in range(DIM)]

    async def create_batch(self, xs):
        return [await self.create(x) for x in xs]


class CE(CrossEncoderClient):
    async def rank(self, query, passages):
        return [(p, 1.0 / (i + 1)) for i, p in enumerate(passages)]


class LLM(LLMClient):
    async def _generate_response(self, *a, **k):
        raise RuntimeError('LLM must not be called')


async def main():
    gid = 'triage1627_' + uuid.uuid4().hex[:8]
    drv = Neo4jDriver('bolt://localhost:7688', 'neo4j', os.environ['NEO4J_PASSWORD'])
    emb = Emb()
    g = Graphiti(
        graph_driver=drv,
        llm_client=LLM.__new__(LLM),
        embedder=emb,
        cross_encoder=CE(),
        tracer=NoOpTracer(),
    )
    await g.build_indices_and_constraints()
    now = datetime.now(timezone.utc)
    ep = EpisodicNode(
        name='ep1',
        group_id=gid,
        source=EpisodeType.text,
        source_description='t',
        content='London is going through a major overhaul to improve its aging infrastructure',
        valid_at=now,
    )
    n = EntityNode(name='London', group_id=gid, summary='A city', labels=['Entity'])
    n.name_embedding = await emb.create(n.name)
    await ep.save(drv)
    await n.save(drv)
    await EpisodicEdge(
        source_node_uuid=ep.uuid, target_node_uuid=n.uuid, group_id=gid, created_at=now
    ).save(drv)
    q = 'London infrastructure overhaul'
    try:
        r = await g.search(q, group_ids=[gid])
        print('search()                     edges ->', [e.fact for e in r])
        r = await g.search_(q, group_ids=[gid])
        print(
            'search_() default            nodes ->',
            [x.name for x in r.nodes],
            '| episodes ->',
            [e.content for e in r.episodes],
            '| edges ->',
            len(r.edges),
        )
        r = await g.search_(q, config=NODE_HYBRID_SEARCH_RRF, group_ids=[gid])
        print(
            'search_(NODE_HYBRID_RRF)     nodes ->',
            [x.name for x in r.nodes],
            '| episodes ->',
            len(r.episodes),
        )
        r = await g.search_(q, config=EDGE_HYBRID_SEARCH_RRF, group_ids=[gid])
        print(
            'search_(EDGE_HYBRID_RRF)     edges ->',
            len(r.edges),
            'nodes ->',
            len(r.nodes),
            'episodes ->',
            len(r.episodes),
        )
        r = await g.search_(q, config=COMBINED_HYBRID_SEARCH_RRF, group_ids=[gid])
        print(
            'search_(COMBINED_RRF)        nodes ->',
            [x.name for x in r.nodes],
            '| episodes ->',
            [e.content for e in r.episodes],
        )
        eps = await get_episodes_by_mentions(drv, [n], [])
        print('get_episodes_by_mentions(node, no edges) ->', [e.content for e in eps])
        r = await g.get_nodes_and_edges_by_episode([ep.uuid])
        print(
            'get_nodes_and_edges_by_episode(ep) nodes ->',
            [x.name for x in r.nodes],
            'edges ->',
            len(r.edges),
        )
        r = await g.retrieve_episodes(now, last_n=5, group_ids=[gid])
        print('retrieve_episodes ->', [e.content for e in r])
    finally:
        await drv.execute_query('MATCH (n {group_id:$g}) DETACH DELETE n', g=gid)
        left, _, _ = await drv.execute_query('MATCH (n {group_id:$g}) RETURN count(n) AS c', g=gid)
        print('cleanup remaining:', left[0]['c'])
        await drv.close()


asyncio.run(main())
