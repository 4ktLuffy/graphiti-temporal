"""The filter in `fix` must not change any decision Graphiti makes from the candidates it is shown.

Runs Graphiti's own `resolve_extracted_edge` twice on random histories: once with every
invalidation candidate, once with only those the filter keeps. The model is deterministic and
chooses duplicates and contradictions by fact identity, so its choices survive the change of
indices: the proof assumes a model's choice about a fact does not depend on which irrelevant facts
sit beside it. The resolved edge, its dates, and the retired facts with their end dates must be
identical; so must `expired_at` and attributes, except where the filter leaves no candidates.

Covers the paths an independent review found the first version of this test missed: semantic
duplicates (the resolved edge takes the duplicate's earlier `valid_at`), dates extracted after the
search when the new edge has none, and naive, UTC and UTC+3 datetimes mixed together.
"""

from __future__ import annotations

import random
import re
from datetime import datetime, timedelta, timezone

import pytest
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.utils.datetime_utils import ensure_utc
from graphiti_core.utils.maintenance.edge_operations import resolve_extracted_edge

from graphiti_temporal.fix import invalidation_candidate_filter
from graphiti_temporal.testing import OracleLLM

UTC = timezone.utc
ZONES = [None, UTC, timezone(timedelta(hours=3))]
BASE = datetime(2000, 1, 1)


class ScriptedModel(OracleLLM):
    """Marks as duplicate / contradicted exactly the facts named in its script."""

    def __init__(self, duplicates: set[str], contradicted: set[str], extracted_start):
        super().__init__()
        self.duplicates, self.contradicted, self.extracted_start = (
            duplicates,
            contradicted,
            extracted_start,
        )

    async def _generate_response(self, messages, response_model=None, *a, **k):
        if getattr(response_model, '__name__', '') == 'EdgeTimestamps':
            start = self.extracted_start
            return {'valid_at': start.isoformat() if start else None, 'invalid_at': None}
        text = messages[-1].content
        items = re.findall(r"\{'idx': (\d+), 'fact': '([^']*)'\}", text)
        return {
            'duplicate_facts': [int(i) for i, f in items if f in self.duplicates],
            'contradicted_facts': [int(i) for i, f in items if f in self.contradicted],
        }


def _day(rng: random.Random, none_rate: float = 0.1) -> datetime | None:
    if rng.random() < none_rate:
        return None
    zone = rng.choice(ZONES)
    return (BASE + timedelta(days=rng.randrange(0, 60))).replace(tzinfo=zone)


def _edge(name: str, valid_at, invalid_at) -> EntityEdge:
    return EntityEdge(
        source_node_uuid='s', target_node_uuid=f't-{name}', group_id='g', name='R',
        fact=f'fact {name}', episodes=[], created_at=BASE.replace(tzinfo=UTC),
        valid_at=valid_at, invalid_at=invalid_at,
    )  # fmt: skip


def _interval(rng: random.Random, name: str) -> EntityEdge:
    start = _day(rng)
    end = None if rng.random() < 0.3 else _day(rng, none_rate=0)
    if start is not None and end is not None and ensure_utc(end) <= ensure_utc(start):
        start, end = end - timedelta(days=1), start  # well-formed: ends after it starts
    return _edge(name, start, end)


def kept(edge: EntityEdge, search_filter) -> bool:
    """Evaluate the filter the way the database does, on UTC-normalized dates."""
    if search_filter.invalid_at is None:
        return True
    cutoff = search_filter.invalid_at[1][0].date
    return edge.invalid_at is None or ensure_utc(edge.invalid_at) > cutoff


async def _decide(new, related, candidates, model):
    episode = EpisodicNode(
        name='e', group_id='g', source=EpisodeType.text, source_description='', content='',
        valid_at=BASE.replace(tzinfo=UTC),
    )  # fmt: skip
    resolved, invalidated, _ = await resolve_extracted_edge(
        model,
        new.model_copy(deep=True),
        [e.model_copy(deep=True) for e in related],
        [e.model_copy(deep=True) for e in candidates],
        episode,
    )
    return (
        resolved.fact,
        ensure_utc(resolved.valid_at),
        ensure_utc(resolved.invalid_at),
        sorted((e.fact, ensure_utc(e.invalid_at)) for e in invalidated),
    ), (resolved.expired_at is not None, resolved.attributes)


def _case(seed: int):
    rng = random.Random(seed)
    new = _edge('new', _day(rng), None)
    related = [_interval(rng, f'dup{i}') for i in range(rng.randrange(0, 4))]
    candidates = [_interval(rng, f'c{i}') for i in range(rng.randrange(1, 12))]
    duplicates = {e.fact for e in related if rng.random() < 0.5}
    contradicted = {e.fact for e in related + candidates if rng.random() < 0.8}
    extracted_start = _day(rng)  # what the model reports if asked for the new edge's dates
    return new, related, candidates, duplicates, contradicted, extracted_start


@pytest.mark.parametrize('seed', range(2000))
async def test_filter_changes_no_decision(seed: int):
    new, related, candidates, duplicates, contradicted, extracted_start = _case(seed)
    search_filter = invalidation_candidate_filter(new, related)
    filtered = [e for e in candidates if kept(e, search_filter)]

    def model():
        return ScriptedModel(duplicates, contradicted, extracted_start)

    full, full_extra = await _decide(new, related, candidates, model())
    kept_result, kept_extra = await _decide(new, related, filtered, model())
    assert kept_result == full
    if related or filtered:
        assert kept_extra == full_extra
    # With nothing left to compare against, Graphiti takes its no-candidate branch
    # (edge_operations.py:653), which skips expired_at bookkeeping and attribute clearing, the
    # inconsistency #1865 reports. The resolved edge, its dates and the retired set still match.


async def test_the_first_version_of_the_filter_was_lossy():
    """Negative control: the new edge's own start alone is not a safe cutoff (review finding)."""
    new = _edge('new', datetime(2000, 1, 21, tzinfo=UTC), None)
    dup = _edge('dup', datetime(2000, 1, 6, tzinfo=UTC), None)
    old = _edge('old', datetime(2000, 1, 1, tzinfo=UTC), datetime(2000, 1, 11, tzinfo=UTC))

    def model():
        return ScriptedModel({dup.fact}, {old.fact}, None)

    full, _ = await _decide(new, [dup], [old], model())
    assert full[3] == [('fact old', datetime(2000, 1, 6, tzinfo=UTC))]
    first_version_kept = [e for e in [old] if ensure_utc(e.invalid_at) > new.valid_at]
    assert (await _decide(new, [dup], first_version_kept, model()))[0] != full
    assert (await _decide(new, [dup], [e for e in [old] if kept(e, invalidation_candidate_filter(new, [dup]))], model()))[0] == full  # fmt: skip


async def test_inverted_interval_is_the_one_exception():
    """A candidate that ends before it starts (invalid_at < valid_at) can end the new fact.

    Graphiti never validates intervals, so such a fact can exist; the filter drops it when its
    `invalid_at` precedes the new fact. Recorded so the exception is known, not hidden.
    """
    new = _edge('new', datetime(2000, 4, 10, tzinfo=UTC), None)
    bad = _edge('bad', datetime(2000, 7, 19, tzinfo=UTC), datetime(2000, 2, 20, tzinfo=UTC))

    def model():
        return ScriptedModel(set(), {bad.fact}, None)

    full, _ = await _decide(new, [], [bad], model())
    assert full[2] == bad.valid_at  # the inverted fact ends the new one
    assert not kept(bad, invalidation_candidate_filter(new, []))
    assert (await _decide(new, [], [], model()))[0][2] is None


async def test_no_candidate_branch_skips_expired_at_bookkeeping():
    """Second exception, found by the second independent review: a new fact that arrives already
    ended (Jan 11-16), whose only candidate ended earlier (Jan 1-6). The filter removes that
    candidate, Graphiti takes its no-candidate branch, and the new fact's `expired_at` is not set.
    Dates and retirements are unchanged; this is the gap #1865 reports and PR #1867 addresses.
    """
    new = _edge('new', datetime(2000, 1, 11, tzinfo=UTC), datetime(2000, 1, 16, tzinfo=UTC))
    old = _edge('old', datetime(2000, 1, 1, tzinfo=UTC), datetime(2000, 1, 6, tzinfo=UTC))

    def model():
        return ScriptedModel(set(), set(), None)

    full, full_extra = await _decide(new, [], [old], model())
    assert not kept(old, invalidation_candidate_filter(new, []))
    filtered, filtered_extra = await _decide(new, [], [], model())
    assert filtered == full
    assert (
        full_extra[0] is True and filtered_extra[0] is False
    )  # expired_at set only with a candidate
