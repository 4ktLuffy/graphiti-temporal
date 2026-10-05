"""Keep facts that already ended out of Graphiti's invalidation-candidate search.

When a new fact arrives, Graphiti searches for facts it might contradict and shows the top 10 to
the model (`resolve_extracted_edges`, `Graphiti.add_triplet`). That search has no filter, so a
subject's superseded history competes with its live facts for the 10 places, and once a subject
has ten or more past values the live one is often not shown and never retired.

The resolved edge is the new edge or a duplicate of it, so its `valid_at` is no earlier than the
earliest start among them. A fact whose `invalid_at` is at or before that start can neither be
invalidated by the resolved edge nor end it: `resolve_edge_contradictions` skips it, and only a
candidate that starts after the resolved edge can end it. Filtering those facts out therefore
changes no decision Graphiti could reach (tests/test_lossless.py, including the duplicate path an
independent review found the first version got wrong); it only frees the places for facts that
matter. A new edge without `valid_at` keeps the unfiltered search.

`apply()` installs this and fix 2 (below) on graphiti-core at runtime; `upstream/01-*.patch` and
`upstream/02-*.patch` are the same changes as diffs against graphiti-core.
`apply_backfill_neighbors()` installs fix 3 (`upstream/03-*.patch`), separately, on top of fix 1.
`Graphiti.add_episode` and `Graphiti.add_triplet` pick it up wherever they were imported. Code that
imported `resolve_extracted_edges` by name before `apply()` keeps the unfiltered original, so call
it through `graphiti_core.utils.maintenance.edge_operations` or call `apply()` first.
"""

from __future__ import annotations

import contextvars
import functools
from datetime import datetime
from typing import Any

from graphiti_core.edges import EntityEdge
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from graphiti_core.utils.datetime_utils import ensure_utc

_cutoffs: contextvars.ContextVar[dict[tuple[str, str], list[datetime | None]] | None] = (
    contextvars.ContextVar('graphiti_temporal_cutoffs', default=None)
)
_applied = False


def invalidation_candidate_filter(
    extracted_edge: EntityEdge, duplicate_candidates: list[EntityEdge]
) -> SearchFilters:
    """Facts still open, or ending after the earliest start the resolved edge can have.

    The resolved edge is the extracted edge or one of its duplicate candidates, so its
    `valid_at` is no earlier than the earliest of theirs. Without `valid_at` on the extracted
    edge, its dates may still be extracted after the search, so nothing is filtered.
    """
    if extracted_edge.valid_at is None:
        return SearchFilters()
    return _filter_from(
        [extracted_edge.valid_at, *(e.valid_at for e in duplicate_candidates if e.valid_at)]
    )


def _filter_from(starts: list[datetime | None]) -> SearchFilters:
    if not starts or any(s is None for s in starts):
        return SearchFilters()
    cutoff = min(ensure_utc(s) for s in starts)  # type: ignore[type-var]
    return SearchFilters(
        invalid_at=[
            [DateFilter(comparison_operator=ComparisonOperator.is_null)],
            [DateFilter(date=cutoff, comparison_operator=ComparisonOperator.greater_than)],
        ]
    )


def _with_edges(edges: list[EntityEdge]):
    """Record, per (fact text, group), the start of the edge being resolved.

    The wrapped search only sees the query text, so two edges with the same text in the same group
    cannot be told apart; for those the search stays unfiltered, exactly as in Graphiti. (Pooling
    their dates was tried and is wrong: an independent review showed one edge's duplicate can lower
    the other's cutoff and, under the result limit, push its live fact out.)
    """
    known: dict[tuple[str, str], list[datetime | None]] = {}
    shared: set[tuple[str, str]] = set()
    for e in edges:
        key = (e.fact, e.group_id)
        if key in known:
            shared.add(key)
        known[key] = [e.valid_at]
    for key in shared:
        known[key] = [None]  # no cutoff: Graphiti's original search
    return _cutoffs.set(known)


def _wrap_search(search):
    @functools.wraps(search)
    async def wrapped(clients, query, *args: Any, **kwargs: Any):
        known = _cutoffs.get()
        group_ids = kwargs.get('group_ids')
        key = (query, group_ids[0]) if known and group_ids and len(group_ids) == 1 else None
        search_filter = kwargs.get('search_filter')
        if key is None or key not in known or search_filter is None:
            return await search(clients, query, *args, **kwargs)
        if search_filter == SearchFilters():
            # The invalidation-candidate search for an edge being resolved.
            kwargs['search_filter'] = _filter_from(known[key])
            return await search(clients, query, *args, **kwargs)
        results = await search(clients, query, *args, **kwargs)
        if search_filter.edge_uuids is not None:
            # The duplicate-candidate search, which Graphiti runs first: a duplicate can become
            # the resolved edge, so its start bounds the cutoff too.
            known[key].extend(e.valid_at for e in results.edges if e.valid_at is not None)
        return results

    wrapped.__graphiti_temporal__ = True  # type: ignore[attr-defined]
    return wrapped


def apply_invalidation_filter() -> None:
    """Fix 1: search invalidation candidates among facts the new fact can affect. Idempotent."""
    global _applied
    if _applied:
        return
    from graphiti_core import graphiti as g
    from graphiti_core.utils.maintenance import edge_operations as eo

    eo.search = _wrap_search(eo.search)
    g.search = _wrap_search(g.search)

    resolve = eo.resolve_extracted_edges

    @functools.wraps(resolve)
    async def resolve_extracted_edges(clients, extracted_edges, *args: Any, **kwargs: Any):
        token = _with_edges(extracted_edges)
        try:
            return await resolve(clients, extracted_edges, *args, **kwargs)
        finally:
            _cutoffs.reset(token)

    eo.resolve_extracted_edges = resolve_extracted_edges
    g.resolve_extracted_edges = resolve_extracted_edges

    add_triplet = g.Graphiti.add_triplet

    @functools.wraps(add_triplet)
    async def add_triplet_filtered(self, source_node, edge, target_node, *args: Any, **kw: Any):
        token = _with_edges([edge])
        try:
            return await add_triplet(self, source_node, edge, target_node, *args, **kw)
        finally:
            _cutoffs.reset(token)

    g.Graphiti.add_triplet = add_triplet_filtered
    _applied = True


_unlimited_index: contextvars.ContextVar[bool] = contextvars.ContextVar(
    'graphiti_temporal_unlimited_index', default=False
)
_fulltext_applied = False


def apply_fulltext_filter_order() -> None:
    """Fix 2: with a search filter, the fulltext index returns every hit before filtering. Idempotent.

    Graphiti's fulltext edge search asks the index for its top `limit` hits and applies the
    filter afterwards, so a filter can be left with nothing (upstream.patch 02 is the same change).
    """
    global _fulltext_applied
    if _fulltext_applied:
        return
    from graphiti_core.search import search as search_module
    from graphiti_core.search import search_utils as su
    from graphiti_core.search.search_filters import edge_search_filter_query_constructor

    relationships_query = su.get_relationships_query

    def get_relationships_query(name: str, limit: int | None, provider) -> str:
        if limit is not None and not _unlimited_index.get():
            return relationships_query(name, limit, provider)
        limited = relationships_query(name, 1, provider)
        return limited.replace(', {limit: $limit}', '').replace(', TOP := $limit', '')

    su.get_relationships_query = get_relationships_query
    fulltext = su.edge_fulltext_search

    @functools.wraps(fulltext)
    async def edge_fulltext_search(driver, query, search_filter, *args: Any, **kwargs: Any):
        filters, _ = edge_search_filter_query_constructor(search_filter, driver.provider)
        token = _unlimited_index.set(bool(filters))
        try:
            return await fulltext(driver, query, search_filter, *args, **kwargs)
        finally:
            _unlimited_index.reset(token)

    su.edge_fulltext_search = edge_fulltext_search
    search_module.edge_fulltext_search = edge_fulltext_search
    _fulltext_applied = True


TEMPORAL_NEIGHBORS = 3
OPEN_SCAN = 50
_neighbor_driver: contextvars.ContextVar[Any] = contextvars.ContextVar(
    'graphiti_temporal_neighbor_driver', default=None
)
_neighbors_applied = False


async def _nearest_own_edges(driver, edge, start, later, exclude, name, limit):
    """The subject's facts starting after `start`, or open at `start`, nearest first.

    Only `>` and `IS NULL` reach the database: FalkorDB 4.x ignores the bound of a string `<` or
    `<=` under a range index, so "started at or before `start`" is checked here.
    """
    from graphiti_core.edges import get_entity_edge_from_record
    from graphiti_core.models.edges.edge_db_queries import get_entity_edge_return_query

    where = 'e.valid_at > $start' if later else '(e.invalid_at IS NULL OR e.invalid_at > $start)'
    if name is not None:
        where += ' AND e.name = $name'
    records, _, _ = await driver.execute_query(
        'MATCH (n:Entity {uuid: $source_uuid})-[e:RELATES_TO {group_id: $group_id}]->(m:Entity) '
        f'WHERE {where} AND NOT e.uuid IN $exclude '
        f'RETURN {get_entity_edge_return_query(driver.provider)} '
        'ORDER BY e.valid_at ASC LIMIT $limit',
        source_uuid=edge.source_node_uuid,
        group_id=edge.group_id,
        start=start,
        name=name,
        exclude=exclude,
        limit=limit if later else OPEN_SCAN,
        routing_='r',
    )
    edges = [get_entity_edge_from_record(r, driver.provider) for r in records]
    if later:
        return edges
    return [e for e in edges if e.valid_at is not None and ensure_utc(e.valid_at) <= start][::-1][
        :limit
    ]


async def temporal_neighbors(driver, extracted_edge: EntityEdge, candidates: list[EntityEdge],
                             limit: int = TEMPORAL_NEIGHBORS) -> list[EntityEdge]:  # fmt: skip
    """Fix 3: the subject's facts nearest in time to the edge being resolved, if search missed them.

    The same function as upstream/03-backfill-temporal-neighbors.patch: up to `limit` facts
    starting after the edge and up to `limit` open at its start, same relation name first.
    Neo4j and FalkorDB only; nothing is added without `valid_at`.
    """
    from graphiti_core.driver.driver import GraphProvider

    if extracted_edge.valid_at is None or driver.provider not in (
        GraphProvider.NEO4J,
        GraphProvider.FALKORDB,
    ):
        return []
    start = ensure_utc(extracted_edge.valid_at)
    exclude = [e.uuid for e in candidates] + [extracted_edge.uuid]
    for name in (extracted_edge.name, None):
        found = [
            *await _nearest_own_edges(driver, extracted_edge, start, True, exclude, name, limit),
            *await _nearest_own_edges(driver, extracted_edge, start, False, exclude, name, limit),
        ]
        if found:
            return found
    return []


def apply_backfill_neighbors() -> None:
    """Fix 3: show the model the subject's temporal neighbours too. Idempotent; use with fix 1.

    Graphiti resolves each edge in `resolve_extracted_edge`, which does not see the driver, so
    the driver is passed in a context variable set by `resolve_extracted_edges` and
    `Graphiti.add_triplet`.
    """
    global _neighbors_applied
    if _neighbors_applied:
        return
    from graphiti_core import graphiti as g
    from graphiti_core.utils.maintenance import edge_operations as eo

    resolve_one = eo.resolve_extracted_edge

    @functools.wraps(resolve_one)
    async def resolve_extracted_edge(llm_client, extracted_edge, related_edges, existing_edges,
                                     *args: Any, **kwargs: Any):  # fmt: skip
        driver = _neighbor_driver.get()
        if driver is not None:
            extra = await temporal_neighbors(
                driver, extracted_edge, [*related_edges, *existing_edges]
            )
            existing_edges = [*existing_edges, *extra]
        return await resolve_one(
            llm_client, extracted_edge, related_edges, existing_edges, *args, **kwargs
        )

    eo.resolve_extracted_edge = resolve_extracted_edge
    g.resolve_extracted_edge = resolve_extracted_edge

    resolve_many = eo.resolve_extracted_edges

    @functools.wraps(resolve_many)
    async def resolve_extracted_edges(clients, *args: Any, **kwargs: Any):
        token = _neighbor_driver.set(clients.driver)
        try:
            return await resolve_many(clients, *args, **kwargs)
        finally:
            _neighbor_driver.reset(token)

    eo.resolve_extracted_edges = resolve_extracted_edges
    g.resolve_extracted_edges = resolve_extracted_edges

    add_triplet = g.Graphiti.add_triplet

    @functools.wraps(add_triplet)
    async def add_triplet_with_neighbors(self, *args: Any, **kwargs: Any):
        token = _neighbor_driver.set(self.driver)
        try:
            return await add_triplet(self, *args, **kwargs)
        finally:
            _neighbor_driver.reset(token)

    g.Graphiti.add_triplet = add_triplet_with_neighbors
    _neighbors_applied = True


def apply() -> None:
    """Install both fixes in graphiti-core."""
    apply_invalidation_filter()
    apply_fulltext_filter_order()
