import asyncio
import os
import uuid
from datetime import datetime, timezone

from falkordb.asyncio import FalkorDB
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client import LLMClient
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF
from graphiti_core.tracer import NoOpTracer


class E(EmbedderClient):
    async def create(self, input_data):
        return [0.1] * 1024

    async def create_batch(self, input_data_list):
        return [[0.1] * 1024 for _ in input_data_list]


class L(LLMClient):
    def __init__(self):
        pass

    async def _generate_response(self, *a, **k):
        raise RuntimeError('stub')

    async def generate_response(self, *a, **k):
        raise RuntimeError('stub')


class X(CrossEncoderClient):
    async def rank(self, q, p):
        return [(x, 1.0) for x in p]


PORT = int(os.environ.get('FPORT', '6380'))
now = datetime.now(timezone.utc)


async def main():
    run = uuid.uuid4().hex[:8]
    base = f'base{run}'
    g1 = f'g1x{run}'
    g2 = f'g2x{run}'
    fdb = FalkorDB(host='localhost', port=PORT)
    drv = FalkorDriver(falkor_db=fdb, database=base)
    g = Graphiti(
        graph_driver=drv, llm_client=L(), embedder=E(), cross_encoder=X(), tracer=NoOpTracer()
    )
    ids = {}
    for gid in (g1, g2):
        d = drv.clone(database=gid)
        ep = EpisodicNode(
            name=f'ep-{gid}',
            group_id=gid,
            source=EpisodeType.text,
            source_description='t',
            content='hello',
            valid_at=now,
        )
        await asyncio.sleep(2)
        for _ in range(10):
            try:
                await ep.save(d)
                break
            except Exception:
                await asyncio.sleep(2)
        en = EntityNode(
            name=f'Alice {gid}', group_id=gid, summary='alice', name_embedding=[0.1] * 1024
        )
        for _ in range(10):
            try:
                await en.save(d)
                break
            except Exception:
                await asyncio.sleep(2)
        ids[gid] = (ep.uuid, en.uuid)
    exp_ep = {ids[g1][0], ids[g2][0]}
    exp_en = {ids[g1][1], ids[g2][1]}
    res = {}

    async def t(name, coro, exp, f):
        try:
            got = f(await coro)
            res[name] = 'OK' if got == exp else f'WRONG got={len(got)}/{len(exp)}'
        except Exception as e:
            res[name] = f'ERROR {type(e).__name__}: {str(e)[:80]}'

    # Graphiti public APIs, shared driver at base graph
    await t(
        'Graphiti.retrieve_episodes([g1,g2])',
        g.retrieve_episodes(now, group_ids=[g1, g2]),
        exp_ep,
        lambda r: {x.uuid for x in r},
    )
    await t(
        'Graphiti.search_(nodes,[g1,g2])',
        g.search_('Alice', config=NODE_HYBRID_SEARCH_RRF, group_ids=[g1, g2]),
        exp_en,
        lambda r: {x.uuid for x in r.nodes},
    )
    await t(
        'Graphiti.search_(nodes,[g1]) single',
        g.search_('Alice', config=NODE_HYBRID_SEARCH_RRF, group_ids=[g1]),
        {ids[g1][1]},
        lambda r: {x.uuid for x in r.nodes},
    )
    # direct node APIs (what MCP get_episodes / node reads call), shared driver bound to base
    await t(
        'EpisodicNode.get_by_group_ids(g.driver,[g1,g2])  [MCP get_episodes path]',
        EpisodicNode.get_by_group_ids(g.driver, [g1, g2]),
        exp_ep,
        lambda r: {x.uuid for x in r},
    )
    await t(
        'EntityNode.get_by_group_ids(g.driver,[g1,g2])',
        EntityNode.get_by_group_ids(g.driver, [g1, g2]),
        exp_en,
        lambda r: {x.uuid for x in r},
    )
    # shared driver rebound to g2 (the issue's post-add_episode state)
    d2 = drv.clone(database=g2)
    await t(
        'EpisodicNode.get_by_group_ids(driver@g2,[g1,g2])',
        EpisodicNode.get_by_group_ids(d2, [g1, g2]),
        exp_ep,
        lambda r: {x.uuid for x in r},
    )
    await t(
        'EpisodicNode.get_by_group_ids(driver@g2,[g1])',
        EpisodicNode.get_by_group_ids(d2, [g1]),
        {ids[g1][0]},
        lambda r: {x.uuid for x in r},
    )
    # does add_episode-style scope mutate shared driver?
    res['shared driver mutated by reads'] = str(g.driver._database != base)
    for k, v in res.items():
        print(f'{v:40s} {k}')
    for gid in (base, g1, g2):
        try:
            await fdb.select_graph(gid).delete()
        except Exception:
            pass


asyncio.run(main())
