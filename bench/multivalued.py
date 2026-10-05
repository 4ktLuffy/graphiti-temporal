"""What showing the model more candidates costs on a relation with many true values.

Alice likes 12 things at once (all open, no contradictions). A 13th like is back-filled into the
middle of that history through `resolve_extracted_edges`. A correct model ends nothing. A model
that wrongly calls shown facts contradictions ends whatever it is shown, so every extra candidate
is extra exposure. Two judges from graphiti_gate.judges:

- `oracle`: the truth; any ended like is a bug in Graphiti's code.
- `over_eager`: calls every other fact about Alice a contradiction; the count of ended likes is
  how many of her likes the model was shown.

Run inside a revision's virtualenv (fix 1 alone, and fix 1 + upstream/03):

    NEO4J_PASSWORD=... <revision python> bench/multivalued.py --trials 20
"""

import argparse
import asyncio
import json
import os
import random
import uuid
from datetime import datetime, timezone

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.utils.maintenance import edge_operations

from graphiti_gate.compat import build_indices, make_clients
from graphiti_gate.judges import Claim, Judge
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, hash_embedding

UTC = timezone.utc
THINGS = ['jazz', 'chess', 'tennis', 'sushi', 'hiking', 'opera', 'cycling', 'poetry', 'baking',
          'sailing', 'karate', 'pottery', 'surfing', 'origami', 'skiing', 'gardening']  # fmt: skip


async def trial(driver, seed: int, persona: str) -> dict:
    rng = random.Random(seed)
    group = f'mv-{uuid.uuid4().hex[:10]}'
    things = rng.sample(THINGS, 13)
    years = sorted(rng.sample(range(1950, 2020), 13))
    late = rng.randrange(1, 12)  # back-filled somewhere inside the history
    now = datetime.now(UTC)
    # Each like is its own slot, so the oracle never sees a contradiction between them.
    claims = {f'Alice likes {t}': Claim('Alice', f'likes:{t}', t) for t in things}
    clients = make_clients(driver=driver, llm_client=Judge(persona=persona, claims=claims),
                           embedder=HashEmbedder(), cross_encoder=NullCrossEncoder())  # fmt: skip
    nodes = {}

    async def node(name):
        if name not in nodes:
            nodes[name] = EntityNode(
                name=name, group_id=group, labels=['Entity'], name_embedding=hash_embedding(name)
            )
            await nodes[name].save(driver)
        return nodes[name]

    def edge(i):
        fact = f'Alice likes {things[i]}'
        return EntityEdge(source_node_uuid=nodes['Alice'].uuid, target_node_uuid=nodes[things[i]].uuid,
                          group_id=group, name='LIKES', fact=fact, fact_embedding=hash_embedding(fact),
                          episodes=[], created_at=now, valid_at=datetime(years[i], 1, 1, tzinfo=UTC))  # fmt: skip

    for t in ['Alice', *things]:
        await node(t)
    stored = []
    for i in range(13):
        if i != late:
            e = edge(i)
            await e.save(driver)
            stored.append(e)
    new = edge(late)
    episode = EpisodicNode(name='late', group_id=group, source=EpisodeType.text, source_description='mv',
                           content=new.fact, valid_at=new.valid_at)  # fmt: skip
    resolved, invalidated, *_ = await edge_operations.resolve_extracted_edges(
        clients, [new], episode, list(nodes.values()), {}, {}
    )
    await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
    return {'seed': seed, 'persona': persona, 'ended_existing': len(invalidated),
            'new_ended': resolved[0].invalid_at is not None}  # fmt: skip


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=20)
    args = ap.parse_args()
    driver = Neo4jDriver(os.environ.get('NEO4J_URI', 'bolt://localhost:7688'),
                         os.environ.get('NEO4J_USER', 'neo4j'), os.environ['NEO4J_PASSWORD'])  # fmt: skip
    await build_indices(driver)
    for persona in ('oracle', 'over_eager'):
        rows = [await trial(driver, s, persona) for s in range(args.trials)]
        for r in rows:
            print(json.dumps(r), flush=True)
        print(json.dumps({'persona': persona,
                          'mean_ended_existing': sum(r['ended_existing'] for r in rows) / len(rows),
                          'new_ended': sum(r['new_ended'] for r in rows), 'trials': len(rows)}), flush=True)  # fmt: skip
    await driver.close()


if __name__ == '__main__':
    asyncio.run(main())
