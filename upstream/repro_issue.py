"""Graphiti does not retire the current fact once a subject has ~10 superseded facts.

Self-contained: graphiti-core and Neo4j only, the LLM replaced by a stub that marks every shown
"Alice lives in X" fact as contradicted (a perfect judge), so any miss is the search's.
Run: NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=... python repro_issue.py
"""

import asyncio
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.tracer import NoOpTracer
from graphiti_core.utils.maintenance.edge_operations import resolve_extracted_edges

UTC = timezone.utc


def vec(text):  # deterministic bag-of-words embedding: facts sharing words are similar
    v = [0.0] * 64
    for w in re.findall(r'\w+', text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class Embedder(EmbedderClient):
    async def create(self, input_data):
        return vec(input_data if isinstance(input_data, str) else ' '.join(input_data))

    async def create_batch(self, input_data_list):
        return [vec(x) for x in input_data_list]


class NoRerank(CrossEncoderClient):
    async def rank(self, query, passages):
        return [(p, 0.0) for p in passages]


class PerfectJudge(LLMClient):
    """Marks every shown 'Alice lives in/has made her home in ...' fact as contradicted."""

    def __init__(self):
        super().__init__(LLMConfig(), cache=False)
        self.shown = []

    async def _generate_response(self, messages, response_model=None, *args, **kwargs):
        if response_model.__name__ == 'EdgeTimestamps':
            return {'valid_at': None, 'invalid_at': None}
        items = re.findall(r"\{'idx': (\d+), 'fact': '([^']*)'\}", messages[-1].content)
        self.shown = [fact for _, fact in items]
        alice = [int(i) for i, fact in items if fact.startswith('Alice') and 'Berlin' not in fact]
        return {'duplicate_facts': [], 'contradicted_facts': alice}


async def main(past_homes: int):
    driver = Neo4jDriver(
        os.environ['NEO4J_URI'], os.environ['NEO4J_USER'], os.environ['NEO4J_PASSWORD']
    )
    await driver.build_indices_and_constraints()
    group, now = f'repro-{uuid.uuid4().hex[:8]}', datetime.now(UTC)
    judge = PerfectJudge()
    clients = GraphitiClients(driver=driver, llm_client=judge, embedder=Embedder(),
                              cross_encoder=NoRerank(), tracer=NoOpTracer())  # fmt: skip
    alice = EntityNode(name='Alice', group_id=group, labels=['Entity'], name_embedding=vec('Alice'))
    await alice.save(driver)

    async def city_edge(city, fact, start, end=None):
        node = EntityNode(name=city, group_id=group, labels=['Entity'], name_embedding=vec(city))
        await node.save(driver)
        edge = EntityEdge(
            source_node_uuid=alice.uuid, target_node_uuid=node.uuid, group_id=group,
            name='LIVES_IN', fact=fact, fact_embedding=vec(fact), episodes=[], created_at=now,
            valid_at=start, invalid_at=end, expired_at=now if end else None,
        )  # fmt: skip
        await edge.save(driver)
        return node, edge

    for i in range(past_homes):  # superseded history: each home ended when the next began
        await city_edge(f'City{i}', f'Alice lives in City{i}',
                        datetime(1950 + 2 * i, 1, 1, tzinfo=UTC), datetime(1952 + 2 * i, 1, 1, tzinfo=UTC))  # fmt: skip
    _, current = await city_edge('Tokyo', 'Alice has made her home in Tokyo since 2019',
                                 datetime(2019, 1, 1, tzinfo=UTC))  # fmt: skip
    berlin = EntityNode(
        name='Berlin', group_id=group, labels=['Entity'], name_embedding=vec('Berlin')
    )
    await berlin.save(driver)
    moved = datetime(2024, 6, 1, tzinfo=UTC)
    new = EntityEdge(source_node_uuid=alice.uuid, target_node_uuid=berlin.uuid, group_id=group,
                     name='LIVES_IN', fact='Alice lives in Berlin', fact_embedding=vec('Alice lives in Berlin'),
                     episodes=[], created_at=now, valid_at=moved)  # fmt: skip
    episode = EpisodicNode(name='move', group_id=group, source=EpisodeType.text, source_description='repro',
                           content='Alice moved to Berlin.', valid_at=moved)  # fmt: skip

    _, invalidated, _ = await resolve_extracted_edges(
        clients, [new], episode, [alice, berlin], {}, {}
    )
    retired = any(e.uuid == current.uuid for e in invalidated)
    expired_shown = sum(f.startswith('Alice lives in City') for f in judge.shown)
    print(f'{past_homes:>2} past homes: shown {len(judge.shown)} candidates ({expired_shown} already ended); '
          f'current home shown={current.fact in judge.shown}, retired={retired}')  # fmt: skip
    await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
    await driver.close()


if __name__ == '__main__':
    for n in (5, 20):
        asyncio.run(main(n))
