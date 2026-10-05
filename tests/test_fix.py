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


class NeighborDriver:
    """Answers fix 3's queries from a list of edges, honouring only `>` and `IS NULL`."""

    def __init__(self, edges, provider=GraphProvider.NEO4J):
        self.edges, self.provider, self.queries = edges, provider, []

    async def execute_query(self, query, **params):
        self.queries.append(query)
        start, name, exclude = params['start'], params['name'], set(params['exclude'])
        later = 'e.valid_at > $start' in query
        rows = [e for e in self.edges if e.uuid not in exclude and (name is None or e.name == name)
                and (e.valid_at > start if later else (e.invalid_at is None or e.invalid_at > start))]  # fmt: skip
        return sorted(rows, key=lambda e: e.valid_at)[: params['limit']], None, None


def _home(year: int, end: int | None = None, name: str = 'LIVES_IN') -> EntityEdge:
    return EntityEdge(source_node_uuid='alice', target_node_uuid=f'c{year}', name=name, group_id='g',
                      fact=f'home {year}', episodes=[], created_at=datetime.now(UTC),
                      valid_at=datetime(year, 1, 1, tzinfo=UTC),
                      invalid_at=datetime(end, 1, 1, tzinfo=UTC) if end else None)  # fmt: skip


@pytest.mark.asyncio
async def test_fix3_neighbors_are_the_next_facts_and_the_one_open_at_start(monkeypatch):
    monkeypatch.setattr('graphiti_core.edges.get_entity_edge_from_record', lambda r, p: r)
    history = [_home(y, y + 2) for y in range(1950, 1980, 2)] + [_home(1980)]
    new = _home(1961)
    found = await fix.temporal_neighbors(NeighborDriver(history), new, [])
    assert [e.fact for e in found] == ['home 1962', 'home 1964', 'home 1966', 'home 1960']


@pytest.mark.asyncio
async def test_fix3_adds_nothing_without_valid_at_or_on_other_providers():
    undated = _home(1961).model_copy(update={'valid_at': None})
    assert await fix.temporal_neighbors(NeighborDriver([_home(1960)]), undated, []) == []
    kuzu = NeighborDriver([_home(1960)], provider=GraphProvider.KUZU)
    assert await fix.temporal_neighbors(kuzu, _home(1961), []) == []
    assert kuzu.queries == []


@pytest.mark.asyncio
async def test_fix3_runtime_passes_neighbors_to_the_resolver(monkeypatch):
    from graphiti_core import graphiti as g

    monkeypatch.setattr('graphiti_core.edges.get_entity_edge_from_record', lambda r, p: r)
    seen = {}

    async def resolver_underneath(llm, edge, related, existing, *a, **k):
        seen['existing'] = [e.fact for e in existing]
        return edge, [], []

    # Install fix 3 afresh on top of a stand-in for Graphiti's resolver; monkeypatch restores all.
    monkeypatch.setattr(fix, '_neighbors_applied', False)
    monkeypatch.setattr(edge_operations, 'resolve_extracted_edge', resolver_underneath)
    monkeypatch.setattr(g, 'resolve_extracted_edge', resolver_underneath)
    monkeypatch.setattr(
        edge_operations, 'resolve_extracted_edges', edge_operations.resolve_extracted_edges
    )
    monkeypatch.setattr(g, 'resolve_extracted_edges', g.resolve_extracted_edges)
    monkeypatch.setattr(g.Graphiti, 'add_triplet', g.Graphiti.add_triplet)
    fix.apply_backfill_neighbors()

    await edge_operations.resolve_extracted_edge(None, _home(1961), [], [_home(1970)], None)
    assert seen['existing'] == ['home 1970']  # no driver in context: nothing added

    token = fix._neighbor_driver.set(NeighborDriver([_home(1960, 1962), _home(1962)]))
    try:
        await edge_operations.resolve_extracted_edge(None, _home(1961), [], [], None)
    finally:
        fix._neighbor_driver.reset(token)
    assert seen['existing'] == ['home 1962', 'home 1960']


@pytest.mark.asyncio
async def test_fix3_leaves_an_ordinary_update_alone(monkeypatch):
    """A move to a new home has nothing later; fix 3 must add no candidates (not even a job)."""
    monkeypatch.setattr('graphiti_core.edges.get_entity_edge_from_record', lambda r, p: r)
    job = _home(1990, name='WORKS_AT')
    history = [_home(1950, 1960), _home(1960), job]
    assert await fix.temporal_neighbors(NeighborDriver(history), _home(1970), []) == []
