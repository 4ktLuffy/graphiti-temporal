"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

# Regression tests for two temporal faults, written to drop into graphiti/tests/.
#
# - OR-ed date filters (#488, fixed by PR #1596): each OR group must keep its own dates.
# - History depth (#1956, fixed by PR #1957): the current fact is retired however long the
#   subject's history is.
# - Back-fill at depth (upstream/03, on top of #1957): an older fact added late ends where the
#   next one begins, however many of the subject's facts compete for the candidate places.
#
# The first test needs no database. The others run on every driver in `tests/helpers_test.py`.

import hashlib
import re
from datetime import datetime, timezone

import pytest
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.driver import GraphProvider
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.graphiti_types import GraphitiClients
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_filters import (
    ComparisonOperator,
    DateFilter,
    SearchFilters,
    edge_search_filter_query_constructor,
)
from graphiti_core.search.search_utils import edge_similarity_search
from graphiti_core.tracer import NoOpTracer
from graphiti_core.utils.maintenance.edge_operations import resolve_extracted_edges
from tests.helpers_test import group_id

pytest_plugins = ('pytest_asyncio',)

UTC = timezone.utc
BEFORE_1997 = DateFilter(
    date=datetime(1997, 1, 1, tzinfo=UTC), comparison_operator=ComparisonOperator.less_than
)
AFTER_1998 = DateFilter(
    date=datetime(1998, 1, 1, tzinfo=UTC), comparison_operator=ComparisonOperator.greater_than
)


def vec(text: str) -> list[float]:
    """Deterministic bag-of-words embedding: facts that share words are similar."""
    v = [0.0] * 64
    for w in re.findall(r'\w+', text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def test_or_date_groups_keep_their_own_dates():
    filters = SearchFilters(valid_at=[[BEFORE_1997], [AFTER_1998]])
    queries, params = edge_search_filter_query_constructor(filters, GraphProvider.NEO4J)
    used = re.findall(r'\$(valid_at_\w+)', ' '.join(queries))
    assert sorted(params[p] for p in used) == [BEFORE_1997.date, AFTER_1998.date]


async def _clean(driver) -> None:
    # The fixture's clear_data does not reach FalkorDB's default graph when the group id differs
    # from the database name, so data left by an earlier test would leak into this one.
    await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group_id)


async def _node(driver, name: str) -> EntityNode:
    node = EntityNode(name=name, group_id=group_id, labels=['Entity'], name_embedding=vec(name))
    await node.save(driver)
    return node


async def _edge(driver, src, dst, fact, start, end=None) -> EntityEdge:
    now = datetime.now(UTC)
    edge = EntityEdge(
        source_node_uuid=src.uuid,
        target_node_uuid=dst.uuid,
        group_id=group_id,
        name='LIVES_IN',
        fact=fact,
        fact_embedding=vec(fact),
        episodes=[],
        created_at=now,
        valid_at=start,
        invalid_at=end,
        expired_at=now if end else None,
    )
    await edge.save(driver)
    return edge


@pytest.mark.asyncio
async def test_or_date_filter_excludes_a_fact_between_the_ranges(graph_driver):
    await _clean(graph_driver)
    alice, paris = await _node(graph_driver, 'Alice'), await _node(graph_driver, 'Paris')
    fact = 'Alice lives in Paris'
    await _edge(graph_driver, alice, paris, fact, datetime(1997, 6, 1, tzinfo=UTC))

    found = await edge_similarity_search(
        graph_driver,
        vec(fact),
        None,
        None,
        SearchFilters(valid_at=[[BEFORE_1997], [AFTER_1998]]),
        [group_id],
        min_score=0.0,
    )
    assert [e.fact for e in found] == []  # started mid-1997: neither before 1997 nor after 1998


class _Embedder(EmbedderClient):
    async def create(self, input_data):
        return vec(input_data if isinstance(input_data, str) else ' '.join(input_data))

    async def create_batch(self, input_data_list):
        return [vec(x) for x in input_data_list]


class _NoRerank(CrossEncoderClient):
    async def rank(self, query, passages):
        return [(p, 0.0) for p in passages]


class _PerfectJudge(LLMClient):
    """Marks every Alice fact it is shown, other than the new one, as contradicted."""

    def __init__(self, new_city: str = 'Berlin'):
        super().__init__(LLMConfig(), cache=False)
        self.new_city = new_city

    async def _generate_response(self, messages, response_model=None, *args, **kwargs):
        if response_model is not None and response_model.__name__ == 'EdgeTimestamps':
            return {'valid_at': None, 'invalid_at': None}
        items = re.findall(r"\{'idx': (\d+), 'fact': '([^']*)'\}", messages[-1].content)
        alice = [int(i) for i, f in items if f.startswith('Alice') and self.new_city not in f]
        return {'duplicate_facts': [], 'contradicted_facts': alice}


@pytest.mark.asyncio
@pytest.mark.parametrize('past_homes', [5, 20])
async def test_current_fact_is_retired_however_long_the_history(graph_driver, past_homes):
    if graph_driver.provider == GraphProvider.KUZU:
        pytest.skip('the Kuzu driver does not create fulltext indexes in build_indices')
    await graph_driver.build_indices_and_constraints()
    await _clean(graph_driver)
    alice = await _node(graph_driver, 'Alice')
    for i in range(past_homes):  # each past home ended when the next began
        city = await _node(graph_driver, f'City{i}')
        start, end = (
            datetime(1950 + 2 * i, 1, 1, tzinfo=UTC),
            datetime(1952 + 2 * i, 1, 1, tzinfo=UTC),
        )
        await _edge(graph_driver, alice, city, f'Alice lives in City{i}', start, end)
    tokyo = await _node(graph_driver, 'Tokyo')
    current = await _edge(graph_driver, alice, tokyo, 'Alice has made her home in Tokyo since 2019',
                          datetime(2019, 1, 1, tzinfo=UTC))  # fmt: skip

    berlin = await _node(graph_driver, 'Berlin')
    moved = datetime(2024, 6, 1, tzinfo=UTC)
    new = EntityEdge(source_node_uuid=alice.uuid, target_node_uuid=berlin.uuid, group_id=group_id,
                     name='LIVES_IN', fact='Alice lives in Berlin', fact_embedding=vec('Alice lives in Berlin'),
                     episodes=[], created_at=datetime.now(UTC), valid_at=moved)  # fmt: skip
    episode = EpisodicNode(name='move', group_id=group_id, source=EpisodeType.text,
                           source_description='test', content='Alice moved to Berlin.', valid_at=moved)  # fmt: skip
    clients = GraphitiClients(driver=graph_driver, llm_client=_PerfectJudge(), embedder=_Embedder(),
                              cross_encoder=_NoRerank(), tracer=NoOpTracer())  # fmt: skip

    result = await resolve_extracted_edges(clients, [new], episode, [alice, berlin], {}, {})
    invalidated = result[1]
    assert current.uuid in {e.uuid for e in invalidated}


@pytest.mark.asyncio
async def test_backfilled_fact_ends_where_the_next_one_begins(graph_driver):
    """A 1961 home added late must end in 1962, and must shorten the 1960 home to 1961.

    The 1960 and 1962 homes, the two that matter, are worded differently from the rest, so ranked
    search prefers the 18 later homes worded like the new fact; with 10 candidate places neither
    is shown, even after fix 1 drops the homes that ended before 1961.
    """
    if graph_driver.provider == GraphProvider.KUZU:
        pytest.skip('the Kuzu driver does not create fulltext indexes in build_indices')
    await graph_driver.build_indices_and_constraints()
    await _clean(graph_driver)
    alice = await _node(graph_driver, 'Alice')
    homes = 25
    for i in range(homes):
        city = await _node(graph_driver, f'City{i}')
        start = datetime(1950 + 2 * i, 1, 1, tzinfo=UTC)
        end = datetime(1952 + 2 * i, 1, 1, tzinfo=UTC) if i < homes - 1 else None
        fact = f'Alice rented a flat in City{i}' if i in (5, 6) else f'Alice lives in City{i}'
        await _edge(graph_driver, alice, city, fact, start, end)

    oslo = await _node(graph_driver, 'Oslo')
    added = datetime(1961, 1, 1, tzinfo=UTC)
    new = EntityEdge(source_node_uuid=alice.uuid, target_node_uuid=oslo.uuid, group_id=group_id,
                     name='LIVES_IN', fact='Alice lives in Oslo', fact_embedding=vec('Alice lives in Oslo'),
                     episodes=[], created_at=datetime.now(UTC), valid_at=added)  # fmt: skip
    episode = EpisodicNode(name='late', group_id=group_id, source=EpisodeType.text,
                           source_description='test', content='In 1961 Alice lived in Oslo.', valid_at=added)  # fmt: skip
    clients = GraphitiClients(driver=graph_driver, llm_client=_PerfectJudge('Oslo'), embedder=_Embedder(),
                              cross_encoder=_NoRerank(), tracer=NoOpTracer())  # fmt: skip

    resolved, invalidated, *_ = await resolve_extracted_edges(
        clients, [new], episode, [alice, oslo], {}, {}
    )
    assert resolved[0].invalid_at == datetime(1962, 1, 1, tzinfo=UTC)  # the next home's start
    shortened = {e.fact: e.invalid_at for e in invalidated}
    assert shortened.get('Alice rented a flat in City5') == added  # the 1960 home now ends in 1961
