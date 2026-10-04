"""Rules any correct temporal memory obeys, checked on random histories (no hand-written outcomes).

Each trial gives one person k homes with known, distinct start dates, plus unrelated history for
another person. It ingests the homes in a random order (so later homes often arrive before earlier
ones), one `resolve_extracted_edges` call per home, persisting the result as `add_episode` does,
with the oracle judge. Then it checks:

- `single_current`: exactly one home is open at the end.
- `chronology`: every home ends exactly where the next one (by start date) begins; the last is open.
- `never_extended`: no step ever moves an existing end date later, or reopens an ended fact.
- `idempotent`: ingesting an already-known home again, same text and dates, changes nothing.

Runs inside a revision's virtualenv: `python -m graphiti_gate.properties --trials 40`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import uuid
from datetime import datetime, timezone

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from graphiti_gate.compat import build_indices, make_clients
from graphiti_gate.judges import Claim, Judge
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, hash_embedding
from graphiti_temporal.world import CITIES

UTC = timezone.utc
TEMPLATES = [
    '{p} lives in {c}',
    '{p} moved to {c}',
    '{p} has made a home in {c}',
    '{p} rents a flat in {c}',
    '{p} is now based in {c}',
]


async def _snapshot(driver, group: str) -> dict[str, tuple]:
    records, _, _ = await driver.execute_query(
        """
        MATCH (a:Entity {name: 'Alice', group_id: $g})-[e:RELATES_TO {group_id: $g}]->()
        RETURN e.fact AS fact, e.invalid_at AS invalid_at, e.expired_at AS expired_at
        """,
        g=group,
    )
    out = {}
    for r in records:
        end = r['invalid_at'].to_native() if r['invalid_at'] is not None else None
        out[r['fact']] = (end.astimezone(UTC) if end else None, r['expired_at'] is not None)
    return out


def history(rng: random.Random, max_homes: int):
    """A random history: homes in date order, the order they arrive in, and the other person's cities."""
    k = rng.randint(2, max_homes)
    cities = rng.sample(CITIES, k + 4)
    years = sorted(rng.sample(range(1900, 2025), k))
    homes = [
        (rng.choice(TEMPLATES).format(p='Alice', c=cities[i]), cities[i], datetime(y, 1, 1, tzinfo=UTC))
        for i, y in enumerate(years)
    ]  # fmt: skip
    order = list(range(k))
    rng.shuffle(order)
    again = rng.randrange(k)
    return homes, order, cities[k : k + 4], again


async def trial(driver, rng: random.Random, max_homes: int) -> dict:
    return await run_history(driver, *history(rng, max_homes))


async def run_history(driver, homes, order, others, again) -> dict:
    """Ingest `homes` in `order` (indices into date-sorted homes); check the rules."""
    group = f'prop-{uuid.uuid4().hex[:12]}'
    now = datetime.now(UTC)
    k = len(homes)
    claims = {fact: Claim('Alice', 'home', city) for fact, city, _ in homes}
    judge = Judge(persona='oracle', claims=claims)
    clients = make_clients(
        driver=driver, llm_client=judge, embedder=HashEmbedder(),
        cross_encoder=NullCrossEncoder(),
    )  # fmt: skip
    from graphiti_core.utils.maintenance import edge_operations

    nodes: dict[str, EntityNode] = {}

    async def node(name: str) -> EntityNode:
        if name not in nodes:
            nodes[name] = EntityNode(
                name=name, group_id=group, labels=['Entity'], name_embedding=hash_embedding(name)
            )
            await nodes[name].save(driver)
        return nodes[name]

    # Unrelated history for someone else, so search has other "lives in" facts to rank.
    for i, city in enumerate(others):
        await EntityEdge(
            source_node_uuid=(await node('Bruno')).uuid, target_node_uuid=(await node(city)).uuid,
            group_id=group, name='LIVES_IN', fact=f'Bruno lives in {city}',
            fact_embedding=hash_embedding(f'Bruno lives in {city}'), episodes=[], created_at=now,
            valid_at=datetime(1980 + 5 * i, 1, 1, tzinfo=UTC),
        ).save(driver)  # fmt: skip

    async def ingest(fact: str, city: str, start: datetime) -> None:
        edge = EntityEdge(
            source_node_uuid=(await node('Alice')).uuid, target_node_uuid=(await node(city)).uuid,
            group_id=group, name='LIVES_IN', fact=fact, fact_embedding=hash_embedding(fact),
            episodes=[], created_at=now, valid_at=start,
        )  # fmt: skip
        episode = EpisodicNode(
            name='p', group_id=group, source=EpisodeType.text, source_description='p',
            content=fact, valid_at=start,
        )  # fmt: skip
        resolved, invalidated, *_ = await edge_operations.resolve_extracted_edges(
            clients, [edge], episode, list(nodes.values()), {}, {}
        )
        for e in [*resolved, *invalidated]:
            e.fact_embedding = e.fact_embedding or hash_embedding(e.fact)
            await e.save(driver)

    never_extended = True
    try:
        before = await _snapshot(driver, group)
        for i in order:
            await ingest(*homes[i])
            after = await _snapshot(driver, group)
            for fact, (end, expired) in before.items():
                new_end, new_expired = after.get(fact, (None, False))
                if (end is not None and (new_end is None or new_end > end)) or (expired and not new_expired):  # fmt: skip
                    never_extended = False
            before = after

        final = before
        open_facts = [f for f, (end, expired) in final.items() if end is None and not expired]
        expected = {
            fact: (homes[i + 1][2] if i + 1 < k else None) for i, (fact, _, _) in enumerate(homes)
        }
        mismatches = [
            {'fact': f, 'start': homes[i][2].date().isoformat(),
             'want_end': end.date().isoformat() if end else None,
             'got_end': (final[f][0].date().isoformat() if final.get(f) and final[f][0] else None)
             if f in final else 'missing',
             'arrived': order.index(i)}
            for i, (f, end) in enumerate(expected.items())
            if final.get(f, ('missing',))[0] != end
        ]  # fmt: skip
        chronology = not mismatches

        await ingest(*homes[again])
        idempotent = await _snapshot(driver, group) == final
        return {
            'homes': k,
            'single_current': len(open_facts) == 1,
            'chronology': chronology,
            'never_extended': never_extended,
            'idempotent': idempotent,
            'open': len(open_facts),
            'mismatches': mismatches[:6],
            'order': order,
        }
    finally:
        await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    logging.getLogger('graphiti_core').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=40)
    ap.add_argument('--max-homes', type=int, default=25)
    ap.add_argument('--seed', type=int, default=0)
    args = ap.parse_args()
    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    await build_indices(driver)
    for t in range(args.trials):
        rng = random.Random(args.seed * 100_000 + t)
        print(json.dumps({'trial': t, **await trial(driver, rng, args.max_homes)}), flush=True)
    await driver.close()


if __name__ == '__main__':
    asyncio.run(main())
