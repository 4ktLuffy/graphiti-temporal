import asyncio
import math
import os
import uuid

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.nodes import EntityNode
from graphiti_core.tracer import NoOpTracer
from graphiti_core.utils.maintenance.node_operations import resolve_extracted_nodes

DIM = 16


def vec(bad=None):
    v = [0.1 * (i + 1) for i in range(DIM)]
    if bad is not None:
        v[3] = bad
    return v


class Emb(EmbedderClient):
    def __init__(self, bad=None):
        self.bad = bad

    async def create(self, input_data):
        return vec(self.bad)

    async def create_batch(self, l):
        return [vec(self.bad) for _ in l]


class StubLLM(LLMClient):
    def __init__(self):
        super().__init__(LLMConfig(api_key='stub', model='stub'))
        self.calls = 0

    async def _generate_response(self, *a, **k):
        self.calls += 1
        return {'entity_resolutions': []}

    async def generate_response(self, *a, **k):
        self.calls += 1
        return {'entity_resolutions': []}


class CE(CrossEncoderClient):
    async def rank(self, q, p):
        return [(x, 1.0) for x in p]


async def run(label, bad, driver, gid_base):
    gid = f'triage1505_{gid_base}_{uuid.uuid4().hex[:6]}'
    seed = EntityNode(name='Alice', group_id=gid, name_embedding=vec())
    await seed.save(driver)
    llm = StubLLM()
    clients = GraphitiClients(
        driver=driver, llm_client=llm, embedder=Emb(bad), cross_encoder=CE(), tracer=NoOpTracer()
    )
    new = EntityNode(name='Alice', group_id=gid)
    try:
        resolved, uuid_map, dups = await resolve_extracted_nodes(clients, [new])
        r = resolved[0]
        print(
            f'[{label}] resolved to seed={r.uuid == seed.uuid} (new node kept={r.uuid == new.uuid}) duplicate_pairs={len(dups)} llm_calls={llm.calls}'
        )
    except Exception as e:
        print(f'[{label}] EXCEPTION {type(e).__name__}: {e}')
    # persistence: save node with bad embedding as add_episode would
    n2 = EntityNode(name='Alice2', group_id=gid, name_embedding=vec(bad))
    try:
        await n2.save(driver)
        recs, _, _ = await driver.execute_query(
            'MATCH (n:Entity {uuid:$u}) RETURN n.name_embedding AS e', u=n2.uuid
        )
        e = recs[0]['e']
        print(
            f'[{label}] persisted embedding contains non-finite: {any(not math.isfinite(x) for x in e)} (sample[3]={e[3]})'
        )
    except Exception as ex:
        print(f'[{label}] save EXCEPTION {type(ex).__name__}: {ex}')
    await driver.execute_query('MATCH (n:Entity {group_id:$g}) DETACH DELETE n', g=gid)


async def main():
    driver = Neo4jDriver('bolt://localhost:7688', 'neo4j', os.environ['NEO4J_PASSWORD'])
    for label, bad in [('clean', None), ('NaN', float('nan')), ('+Inf', float('inf'))]:
        await run(label, bad, driver, label.strip('+'))
    await driver.close()


asyncio.run(main())
