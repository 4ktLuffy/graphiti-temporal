# MCP get_episodes handler against live FalkorDB, no LLM. Needs PYTHONPATH=<wt>:<wt>/mcp_server/src:extra
import asyncio
import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from falkordb.asyncio import FalkorDB
from graphiti_core.driver.falkordb_driver import FalkorDriver
from graphiti_core.nodes import EpisodeType, EpisodicNode

sys.path.insert(0, os.path.join(os.environ['WT'], 'mcp_server'))
from config.schema import GraphitiConfig
import importlib

server = importlib.import_module('src.graphiti_mcp_server')


async def main():
    run = uuid.uuid4().hex[:8]
    base = f'mbase{run}'
    g1 = f'mg1{run}'
    g2 = f'mg2{run}'
    fdb = FalkorDB(host='localhost', port=int(os.environ.get('FPORT', '6380')))
    drv = FalkorDriver(falkor_db=fdb, database=base)
    await asyncio.sleep(1)
    ids = {}
    await asyncio.sleep(2)
    for gid in (g1, g2):
        d = drv.clone(database=gid)
        await asyncio.sleep(2)
        for i in range(2):
            ep = EpisodicNode(
                name=f'e{i}',
                group_id=gid,
                source=EpisodeType.text,
                source_description='t',
                content='c',
                valid_at=datetime.now(timezone.utc),
            )
            for _ in range(10):
                try:
                    await ep.save(d)
                    break
                except Exception:
                    await asyncio.sleep(2)
            ids.setdefault(gid, set()).add(ep.uuid)
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client import LLMClient
    from graphiti_core.tracer import NoOpTracer

    class E(EmbedderClient):
        async def create(self, x):
            return [0.1] * 1024

        async def create_batch(self, x):
            return [[0.1] * 1024 for _ in x]

    class L(LLMClient):
        def __init__(self):
            pass

        async def _generate_response(self, *a, **k):
            raise RuntimeError('stub')

    class X(CrossEncoderClient):
        async def rank(self, q, p):
            return [(x, 1.0) for x in p]

    client = Graphiti(
        graph_driver=drv, llm_client=L(), embedder=E(), cross_encoder=X(), tracer=NoOpTracer()
    )
    server.config = GraphitiConfig()
    server.graphiti_service = SimpleNamespace(get_client=AsyncMock(return_value=client))
    exp = ids[g1] | ids[g2]
    for label, gids, mx, expset in (
        ('multi [g1,g2] max=10', [g1, g2], 10, exp),
        ('multi [g2,g1] max=10', [g2, g1], 10, exp),
        ('single g1', [g1], 10, ids[g1]),
    ):
        r = await server.get_episodes(group_ids=gids, max_episodes=mx)
        got = (
            {e['uuid'] for e in getattr(r, 'episodes', None) or r.get('episodes', [])}
            if not isinstance(r, dict) or 'episodes' in r
            else set()
        )
        print(
            ('OK   ' if got == expset else f'WRONG {len(got)}/{len(expset)} ')
            + f' get_episodes {label}'
        )
    r = await server.get_episodes(group_ids=[g1, g2, g1], max_episodes=10)
    eps = r['episodes'] if isinstance(r, dict) else r.episodes
    print(
        'dup-input dedup:', 'OK' if len(eps) == len(exp) else f'DUPLICATES {len(eps)} vs {len(exp)}'
    )
    r = await server.get_episodes(group_ids=[g1, g2], max_episodes=3)
    eps = r['episodes'] if isinstance(r, dict) else r.episodes
    print('global limit max=3 ->', len(eps), '(expect 3)')
    print('driver unchanged:', drv._database == base)
    for gid in (base, g1, g2):
        try:
            await fdb.select_graph(gid).delete()
        except Exception:
            pass


asyncio.run(main())
