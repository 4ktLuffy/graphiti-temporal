"""The evaluator must fail corrupted graphs that an earlier version passed (third review)."""

from datetime import datetime, timezone

from graphiti_gate.judges import Judge
from graphiti_gate.scenario import Scenario
from graphiti_gate.worker import _evaluate, _expand

UTC = timezone.utc
SCENARIO = Scenario.model_validate({
    'id': 't', 'title': 't', 'source': 't', 'family': 't',
    'facts': [{'id': 'lives', 'src': 'A', 'rel': 'R', 'dst': 'B', 'fact': 'Alice lives in Berlin',
               'valid': '2020-01-01', 'claim': ['A', 'home', 'B']}],
    'incoming': [{'id': 'resides', 'src': 'A', 'rel': 'R', 'dst': 'B', 'fact': 'Alice resides in Berlin',
                  'valid': '2024-01-01', 'claim': ['A', 'home', 'B']}],
    'expect': {'resides': {'merged_into': 'lives'}},
})  # fmt: skip
UUIDS = {'lives': 'u-lives', 'resides': 'u-resides'}
MERGED = {'resides': 'u-lives'}


def _rec(fact, valid, episodes=2):
    return {'fact': fact, 'valid_at': valid, 'invalid_at': None, 'expired_at': None,
            'episodes': ['e'] * episodes}  # fmt: skip


def _run(stored):
    facts, slots = _expand(SCENARIO)
    return _evaluate(SCENARIO, facts, slots, UUIDS, MERGED, stored, Judge())


def test_correct_graph_passes():
    stored = {'u-lives': _rec('Alice lives in Berlin', datetime(2020, 1, 1, tzinfo=UTC))}
    assert _run(stored)['passed']


def test_extra_stored_edge_fails():
    stored = {
        'u-lives': _rec('Alice lives in Berlin', datetime(2020, 1, 1, tzinfo=UTC)),
        'u-extra': _rec('Alice lives in Berlin', datetime(2020, 1, 1, tzinfo=UTC)),
    }
    assert not _run(stored)['passed']


def test_merged_fact_also_stored_fails():
    stored = {
        'u-lives': _rec('Alice lives in Berlin', datetime(2020, 1, 1, tzinfo=UTC)),
        'u-resides': _rec('Alice resides in Berlin', datetime(2024, 1, 1, tzinfo=UTC), 1),
    }
    assert not _run(stored)['passed']


def test_changed_text_or_start_of_target_fails_unless_expected():
    # 'lives' is only a merge target here, so its text and start must not change.
    for rec in (_rec('Alice lived in Berlin', datetime(2020, 1, 1, tzinfo=UTC)),
                _rec('Alice lives in Berlin', datetime(2019, 1, 1, tzinfo=UTC)),
                _rec('Alice lives in Berlin', datetime(2020, 1, 1, tzinfo=UTC), episodes=5)):  # fmt: skip
        assert not _run({'u-lives': rec})['passed']
