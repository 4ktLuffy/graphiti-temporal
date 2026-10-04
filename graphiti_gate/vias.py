"""Scenario modes beyond edge resolution, for the code the first 24 scenarios never ran.

- `search`: a search with `SearchFilters` returns exactly the expected facts (date filters with
  OR groups, #488 / PR #1596).
- `nodes`: entity resolution finds an existing entity by exact name even when the stored name
  embedding disagrees (#1734 / PR #1741).
- `bulk_dedupe`: the same fact in two episodes of one bulk batch becomes one edge citing both
  (#1872 / PR #1873).

Each mode returns `(failures, state)` like the edge-resolution path in `worker.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters

from graphiti_temporal.testing import hash_embedding

UTC = timezone.utc
OPS = {
    '<': ComparisonOperator.less_than, '<=': ComparisonOperator.less_than_equal,
    '>': ComparisonOperator.greater_than, '>=': ComparisonOperator.greater_than_equal,
    '=': ComparisonOperator.equals, 'null': ComparisonOperator.is_null,
    'not null': ComparisonOperator.is_not_null,
}  # fmt: skip


def _date(value) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def search_filter(spec: dict[str, Any]) -> SearchFilters:
    """{'valid_at': [[{'op': '<', 'date': ...}], [...]], ...} -> SearchFilters (OR of ANDs)."""
    kwargs = {}
    for field in ('valid_at', 'invalid_at', 'created_at', 'expired_at'):
        if field in spec:
            kwargs[field] = [
                [DateFilter(date=_date(c.get('date')), comparison_operator=OPS[c['op']]) for c in group]
                for group in spec[field]
            ]  # fmt: skip
    return SearchFilters(**kwargs)


async def run_search(clients, s, group: str, uuids: dict[str, str]) -> tuple[list[str], dict]:
    from graphiti_core.search import search as search_module
    from graphiti_core.search.search_config import (
        EdgeReranker,
        EdgeSearchConfig,
        EdgeSearchMethod,
        SearchConfig,
    )

    methods = {'fulltext': [EdgeSearchMethod.bm25], 'vector': [EdgeSearchMethod.cosine_similarity],
               'hybrid': [EdgeSearchMethod.bm25, EdgeSearchMethod.cosine_similarity]}  # fmt: skip
    q = s.search
    config = SearchConfig(
        edge_config=EdgeSearchConfig(search_methods=methods[q.get('channel', 'hybrid')],
                                     reranker=EdgeReranker.rrf),
        limit=q.get('limit', 10),
    )  # fmt: skip
    result = await search_module.search(clients, q['query'], [group], config, search_filter(q.get('filter', {})))  # fmt: skip
    by_uuid = {v: k for k, v in uuids.items()}
    got = sorted(by_uuid.get(e.uuid, f'?{e.fact}') for e in result.edges)
    want = sorted(q['expect'])
    failures = [] if got == want else [f'search returned {got}, expected {want}']
    return failures, {'returned': got}


async def run_nodes(clients, s, group: str, driver) -> tuple[list[str], dict]:
    from graphiti_core.utils.maintenance import node_operations

    existing = {}
    for n in s.nodes:
        node = EntityNode(
            name=n['name'], group_id=group, labels=['Entity'],
            name_embedding=hash_embedding(n.get('embedding_of', n['name'])),
        )  # fmt: skip
        await node.save(driver)
        existing[n['id']] = node.uuid
    extracted = [
        EntityNode(
            name=name, group_id=group, labels=['Entity'], name_embedding=hash_embedding(name)
        )
        for name in s.incoming_nodes
    ]
    episode = EpisodicNode(
        name='gate', group_id=group, source=EpisodeType.text, source_description='gate',
        content=' '.join(s.incoming_nodes), valid_at=datetime.now(UTC),
    )  # fmt: skip
    resolved, uuid_map, _ = await node_operations.resolve_extracted_nodes(clients, extracted, episode, [])  # fmt: skip
    by_uuid = {v: k for k, v in existing.items()}
    got = {
        name: by_uuid.get(uuid_map.get(e.uuid, e.uuid), 'new')
        for name, e in zip(s.incoming_nodes, extracted, strict=True)
    }
    failures = [
        f'"{name}" resolved to {got[name]}, expected {want}'
        for name, want in s.expect_nodes.items()
        if got.get(name) != want
    ]
    return failures, {'resolved': got}


async def run_bulk_dedupe(
    clients, s, group: str, nodes: dict[str, EntityNode]
) -> tuple[list[str], dict]:
    from graphiti_core.utils import bulk_utils

    now = datetime.now(UTC)
    episodes, per_episode = [], []
    for i, batch in enumerate(s.bulk):
        ep = EpisodicNode(
            name=f'ep{i}', group_id=group, source=EpisodeType.text, source_description='gate',
            content='; '.join(f['fact'] for f in batch), valid_at=now, uuid=str(uuid.uuid4()),
        )  # fmt: skip
        episodes.append(ep)
        edges = []
        for f in batch:
            edges.append(EntityEdge(
                source_node_uuid=nodes[f['src']].uuid, target_node_uuid=nodes[f['dst']].uuid,
                group_id=group, name=f['rel'], fact=f['fact'], fact_embedding=hash_embedding(f['fact']),
                episodes=[ep.uuid], created_at=now, valid_at=_date(f.get('valid')),
            ))  # fmt: skip
        per_episode.append(edges)
    result = await bulk_utils.dedupe_edges_bulk(
        clients, per_episode, [(ep, []) for ep in episodes], list(nodes.values()), {}, {}
    )
    canonical = {}
    for ep in episodes:
        for e in result.get(ep.uuid, []):
            canonical.setdefault(e.fact, set()).add(e.uuid)
    failures = []
    for fact, want in s.expect_bulk.items():
        uuids = canonical.get(fact, set())
        if want.get('one_edge') and len(uuids) != 1:
            failures.append(f'"{fact}" became {len(uuids)} edges, expected one')
        if 'episodes' in want:
            edge = next(
                (e for ep in episodes for e in result.get(ep.uuid, []) if e.fact == fact), None
            )
            cited = len(set(edge.episodes)) if edge else 0
            if cited != want['episodes']:
                failures.append(f'"{fact}" cites {cited} episodes, expected {want["episodes"]}')
    return failures, {'edges_per_fact': {k: len(v) for k, v in canonical.items()}}
