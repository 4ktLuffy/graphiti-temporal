"""A graph with a known history: people who moved home N times, each move a superseded fact.

Every fact uses the same wording ("<person> lives in <city>"), so the live fact is no easier or
harder to retrieve than the history behind it. Insertion order is shuffled per seed, because a
search engine breaking score ties by storage order would otherwise decide the result.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from graphiti_core.driver.driver import GraphDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.utils.datetime_utils import utc_now

from graphiti_temporal.testing import Claim, OracleLLM, hash_embedding

UTC = timezone.utc

CITIES = [
    'Paris', 'Rome', 'Oslo', 'Lima', 'Cairo', 'Delhi', 'Seoul', 'Quito', 'Dublin', 'Vienna',
    'Prague', 'Zurich', 'Madrid', 'Lisbon', 'Athens', 'Warsaw', 'Helsinki', 'Riga', 'Tallinn',
    'Vilnius', 'Bogota', 'Santiago', 'Caracas', 'Havana', 'Manila', 'Hanoi', 'Bangkok', 'Jakarta',
    'Nairobi', 'Accra', 'Dakar', 'Tunis', 'Rabat', 'Muscat', 'Doha', 'Amman', 'Beirut', 'Baku',
    'Tbilisi', 'Yerevan', 'Astana', 'Tashkent', 'Kabul', 'Dhaka', 'Colombo', 'Kathmandu', 'Thimphu',
    'Ulaanbaatar', 'Taipei', 'Osaka', 'Kyoto', 'Busan', 'Perth', 'Hobart', 'Auckland', 'Suva',
    'Apia', 'Honolulu', 'Anchorage', 'Denver', 'Austin', 'Boston', 'Chicago', 'Montreal', 'Calgary',
    'Halifax', 'Reykjavik', 'Bergen', 'Gothenburg', 'Aarhus', 'Hamburg', 'Munich', 'Cologne',
    'Lyon', 'Marseille', 'Porto', 'Seville', 'Valencia', 'Naples', 'Milan', 'Turin', 'Geneva',
    'Basel', 'Antwerp', 'Ghent', 'Utrecht', 'Krakow', 'Gdansk', 'Brno', 'Bratislava', 'Ljubljana',
    'Zagreb', 'Sarajevo', 'Skopje', 'Tirana', 'Sofia', 'Varna', 'Bucharest', 'Chisinau', 'Minsk',
    'Kyiv', 'Odesa', 'Lviv', 'Kazan', 'Samara', 'Perm', 'Omsk', 'Irkutsk', 'Yakutsk', 'Harbin',
    'Dalian', 'Xiamen', 'Wuhan', 'Chengdu', 'Kunming', 'Lhasa', 'Urumqi', 'Lanzhou', 'Hefei',
]  # fmt: skip

PEOPLE = ['Alice', 'Bruno', 'Chen', 'Dara', 'Elif', 'Femi', 'Goran', 'Hana']


def fact(person: str, city: str) -> str:
    return f'{person} lives in {city}'


@dataclass
class World:
    group_id: str
    subject: EntityNode
    live_edge: EntityEdge
    history: list[EntityEdge]
    nodes: dict[str, EntityNode]
    new_valid_at: datetime


async def _node(driver: GraphDriver, nodes: dict[str, EntityNode], name: str, group_id: str):
    if name not in nodes:
        n = EntityNode(
            name=name, group_id=group_id, labels=['Entity'], name_embedding=hash_embedding(name)
        )
        await n.save(driver)
        nodes[name] = n
    return nodes[name]


async def build(
    driver: GraphDriver,
    oracle: OracleLLM,
    *,
    n_history: int,
    seed: int,
    other_people: int = 3,
    other_history: int = 5,
) -> World:
    """Alice has `n_history` past homes and one current home; other people add unrelated history."""
    rng = random.Random(seed)
    # Unique per call, never derived from the seed: two runs sharing a seed must not share a graph.
    group_id = f'hd-{n_history}-{seed}-{uuid.uuid4().hex[:12]}'
    now = utc_now()
    nodes: dict[str, EntityNode] = {}
    plan: list[tuple[str, str, datetime, datetime | None]] = []

    for person in PEOPLE[: 1 + other_people]:
        k = n_history if person == 'Alice' else other_history
        cities = rng.sample(CITIES, k + 1)
        years = [1950 + i * 2 for i in range(k + 1)]
        for i, city in enumerate(cities):
            end = datetime(years[i + 1], 1, 1, tzinfo=UTC) if i < k else None
            plan.append((person, city, datetime(years[i], 1, 1, tzinfo=UTC), end))

    rng.shuffle(plan)
    live_edge: EntityEdge | None = None
    history: list[EntityEdge] = []
    for person, city, start, end in plan:
        src = await _node(driver, nodes, person, group_id)
        dst = await _node(driver, nodes, city, group_id)
        text = fact(person, city)
        oracle.register([(text, Claim(person, 'lives_in', city))])
        edge = EntityEdge(
            source_node_uuid=src.uuid,
            target_node_uuid=dst.uuid,
            group_id=group_id,
            name='LIVES_IN',
            fact=text,
            fact_embedding=hash_embedding(text),
            episodes=[],
            created_at=now,
            valid_at=start,
            invalid_at=end,
            expired_at=now if end else None,
        )
        await edge.save(driver)
        if person == 'Alice':
            if end is None:
                live_edge = edge
            else:
                history.append(edge)

    assert live_edge is not None and live_edge.valid_at is not None
    return World(
        group_id=group_id,
        subject=nodes['Alice'],
        live_edge=live_edge,
        history=history,
        nodes=nodes,
        new_valid_at=datetime(live_edge.valid_at.year + 1, 6, 1, tzinfo=UTC),
    )


async def new_move(driver: GraphDriver, oracle: OracleLLM, world: World, city: str = 'Berlin'):
    """The extracted edge and episode for "Alice moved to <city>", as extraction would produce them."""
    dst = await _node(driver, world.nodes, city, world.group_id)
    text = fact('Alice', city)
    oracle.register([(text, Claim('Alice', 'lives_in', city))])
    episode = EpisodicNode(
        name='move',
        group_id=world.group_id,
        source=EpisodeType.text,
        source_description='test',
        content=f'Alice moved to {city}.',
        valid_at=world.new_valid_at,
    )
    edge = EntityEdge(
        source_node_uuid=world.subject.uuid,
        target_node_uuid=dst.uuid,
        group_id=world.group_id,
        name='LIVES_IN',
        fact=text,
        fact_embedding=hash_embedding(text),
        episodes=[episode.uuid],
        created_at=utc_now(),
        valid_at=world.new_valid_at,
    )
    return edge, episode, [world.subject, dst]
