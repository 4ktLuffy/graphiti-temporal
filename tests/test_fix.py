"""Both runtime fixes, checked the way the upstream patches' tests check them (no database)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from graphiti_core.driver.driver import GraphProvider
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodicNode
from graphiti_core.search import search_utils
from graphiti_core.search.search_config import SearchResults
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from graphiti_core.utils.maintenance import edge_operations

from graphiti_temporal import fix

fix.apply()  # functions are looked up on the modules at call time, so import order is free


UTC = timezone.utc


class RecordingDriver:
    provider = GraphProvider.NEO4J
    search_interface = None
    fulltext_syntax = ''

    def __init__(self):
        self.cypher_query = ''

    async def execute_query(self, cypher_query_: str, **kwargs: Any):
        self.cypher_query = cypher_query_
        return [], None, None


@pytest.mark.parametrize(
    'search_filter',
    [
        SearchFilters(invalid_at=[[DateFilter(comparison_operator=ComparisonOperator.is_null)]]),
        SearchFilters(edge_uuids=['a']),
    ],
)
async def test_fix2_filtered_fulltext_returns_every_index_hit(search_filter):
    driver = RecordingDriver()
    await search_utils.edge_fulltext_search(driver, 'Alice', search_filter, ['g'], limit=20)
    assert '{limit: $limit}' not in driver.cypher_query
    assert 'LIMIT $limit' in driver.cypher_query


async def test_fix2_unfiltered_fulltext_keeps_the_index_limit():
    driver = RecordingDriver()
    await search_utils.edge_fulltext_search(driver, 'Alice', SearchFilters(), ['g'], limit=20)
    assert '{limit: $limit}' in driver.cypher_query


async def test_fix1_invalidation_search_skips_facts_ended_before_the_new_fact(monkeypatch):
    monkeypatch.setattr(
        edge_operations, 'create_entity_edge_embeddings', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    recorded = AsyncMock(return_value=SearchResults())
    # Under the wrapper fix 1 installed, so the wrapper still decides the filter.
    monkeypatch.setattr(edge_operations, 'search', fix._wrap_search(recorded))
    llm = MagicMock()
    llm.generate_response = AsyncMock(
        return_value={'duplicate_facts': [], 'contradicted_facts': []}
    )
    clients = SimpleNamespace(driver=MagicMock(), llm_client=llm, embedder=MagicMock())
    valid_at = datetime(2024, 6, 1, tzinfo=UTC)
    alice = EntityNode(uuid='alice', name='Alice', group_id='g', labels=['Entity'])
    berlin = EntityNode(uuid='berlin', name='Berlin', group_id='g', labels=['Entity'])
    edge = EntityEdge(
        source_node_uuid='alice', target_node_uuid='berlin', name='LIVES_IN', group_id='g',
        fact='Alice lives in Berlin', episodes=[], created_at=valid_at, valid_at=valid_at,
    )  # fmt: skip
    episode = EpisodicNode(
        name='e', group_id='g', source='message', source_description='', content='',
        valid_at=valid_at,
    )  # fmt: skip

    await edge_operations.resolve_extracted_edges(clients, [edge], episode, [alice, berlin], {}, {})

    filters = [c.kwargs['search_filter'] for c in recorded.await_args_list]
    assert fix.invalidation_candidate_filter(edge, []) in filters
    assert SearchFilters() not in filters


def test_fix1_without_valid_at_keeps_the_unfiltered_search():
    edge = EntityEdge(
        source_node_uuid='a', target_node_uuid='b', name='R', group_id='g', fact='f',
        episodes=[], created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )  # fmt: skip
    assert fix.invalidation_candidate_filter(edge, []) == SearchFilters()


async def test_fix1_runtime_cutoff_includes_duplicate_candidates(monkeypatch):
    """Graphiti searches duplicates first; a duplicate's earlier start must bound the cutoff."""
    monkeypatch.setattr(
        edge_operations, 'create_entity_edge_embeddings', AsyncMock(return_value=None)
    )
    monkeypatch.setattr(EntityEdge, 'get_between_nodes', AsyncMock(return_value=[]))
    new_start, dup_start = datetime(2024, 1, 21, tzinfo=UTC), datetime(2024, 1, 6)
    duplicate = EntityEdge(
        source_node_uuid='alice', target_node_uuid='berlin', name='LIVES_IN', group_id='g',
        fact='Alice resides in Berlin', episodes=[], created_at=new_start, valid_at=dup_start,
    )  # fmt: skip

    async def fake_search(clients, query, **kwargs):
        if kwargs['search_filter'].edge_uuids is not None:
            return SearchResults(edges=[duplicate])
        return SearchResults()

    recorded = AsyncMock(side_effect=fake_search)
    monkeypatch.setattr(edge_operations, 'search', fix._wrap_search(recorded))
    llm = MagicMock()
    llm.generate_response = AsyncMock(
        return_value={'duplicate_facts': [], 'contradicted_facts': []}
    )
    clients = SimpleNamespace(driver=MagicMock(), llm_client=llm, embedder=MagicMock())
    edge = EntityEdge(
        source_node_uuid='alice', target_node_uuid='berlin', name='LIVES_IN', group_id='g',
        fact='Alice lives in Berlin', episodes=[], created_at=new_start, valid_at=new_start,
    )  # fmt: skip
    episode = EpisodicNode(
        name='e', group_id='g', source='message', source_description='', content='',
        valid_at=new_start,
    )  # fmt: skip

    alice = EntityNode(uuid='alice', name='Alice', group_id='g', labels=['Entity'])
    berlin = EntityNode(uuid='berlin', name='Berlin', group_id='g', labels=['Entity'])
    await edge_operations.resolve_extracted_edges(clients, [edge], episode, [alice, berlin], {}, {})

    used = recorded.await_args_list[-1].kwargs['search_filter']
    assert used == fix.invalidation_candidate_filter(edge, [duplicate])
    assert used.invalid_at[1][0].date == datetime(2024, 1, 6, tzinfo=UTC)


def test_fix1_runtime_leaves_shared_text_unfiltered():
    """Two edges with the same text and group cannot be told apart by the search wrapper."""
    start = datetime(2024, 1, 21, tzinfo=UTC)
    edges = [
        EntityEdge(
            source_node_uuid=src,
            target_node_uuid='x',
            name='R',
            group_id='g',
            fact='same text',
            episodes=[],
            created_at=start,
            valid_at=start,
        )  # fmt: skip
        for src in ('a', 'b')
    ]
    token = fix._with_edges(edges)
    try:
        assert fix._filter_from(fix._cutoffs.get()[('same text', 'g')]) == SearchFilters()
    finally:
        fix._cutoffs.reset(token)
