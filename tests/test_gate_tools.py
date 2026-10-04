"""Pure pieces of the gate: filters built from scenarios, and the shrinker's index arithmetic."""

from datetime import date, datetime, timezone

from graphiti_core.search.search_filters import ComparisonOperator

from graphiti_gate.shrink import _without_home
from graphiti_gate.vias import search_filter


def test_search_filter_builds_or_of_and_groups():
    f = search_filter(
        {
            'valid_at': [
                [{'op': '<', 'date': date(1995, 1, 1)}],
                [{'op': '>', 'date': date(2005, 1, 1)}],
            ]
        }
    )
    assert [[d.comparison_operator for d in g] for g in f.valid_at] == [
        [ComparisonOperator.less_than],
        [ComparisonOperator.greater_than],
    ]
    assert f.valid_at[0][0].date == datetime(1995, 1, 1, tzinfo=timezone.utc)


def test_without_home_reindexes_arrival_order():
    homes = ['h0', 'h1', 'h2', 'h3']
    order = [3, 1, 0, 2]
    homes2, order2, again2 = _without_home(homes, order, again=3, i=1)
    assert homes2 == ['h0', 'h2', 'h3']
    assert order2 == [2, 0, 1]  # h3 -> 2, h0 -> 0, h2 -> 1
    assert again2 == 2
