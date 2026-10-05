"""The fuzzer's pure parts: workloads stay reproducible, wordings, and how findings are classified."""

import json
from pathlib import Path

from graphiti_gate.fuzz import Op, workload
from graphiti_gate.fuzz_shrink import kind

CASES = Path(__file__).resolve().parent.parent / 'graphiti_gate' / 'fuzz_cases'


def test_varied_changes_only_the_wording():
    for seed in (0, 7, 3000):
        plain, varied = workload(seed, deep=True), workload(seed, deep=True, varied=True)
        strip = [(o.via, o.subject, o.slot, o.value, o.year, o.reworded) for o in plain]
        assert strip == [(o.via, o.subject, o.slot, o.value, o.year, o.reworded) for o in varied]
        assert {o.wording for o in plain} == {0}


def test_varied_uses_all_five_wordings():
    phrases = {o.fact.split(' ', 1)[1].rsplit(' ', 1)[0]
               for s in range(20) for o in workload(s, deep=True, varied=True) if o.slot == 'home'}  # fmt: skip
    assert phrases == {
        'lives in',
        'resides in',
        'has made a home in',
        'rents a flat in',
        'is based in',
    }


def test_saved_cases_still_load_and_word_the_same():
    for path in CASES.glob('*.json'):
        case = json.loads(path.read_text())
        ops = [Op(**o) for o in case['ops']]
        assert all(o.fact for o in ops)
        assert all(o.wording == 0 for o in ops)  # saved before `wording` existed


def test_kind_tells_findings_apart():
    def rule(detail):
        return {'oracle': 'rule', 'backend': 'neo4j', 'detail': detail}

    assert (
        kind(rule('search as of Alice started before 1997 or after 1998: got [x]'))
        == 'or-date-search'
    )
    assert kind(rule('search as of Alice@1999: got [x], stored state says []')) == 'as-of-search'
    assert kind(rule('Alice/home: 2 current values [a, b]')) == 'two-current'
    assert kind(rule('Alice/home: "a" ends 2008-01-01, next starts 2006-01-01')) == 'interval-chain'
    assert kind(rule('crash: ValueError')) == 'crash'
    assert (
        kind(rule('Alice: "a" has an empty or inverted interval 2001 to 2001'))
        == 'inverted-interval'
    )
    assert kind({'oracle': 'backends', 'backend': 'kuzu vs neo4j', 'detail': {}}) == 'backends'


def test_crowded_backfills_one_home_whose_neighbours_are_worded_apart():
    from graphiti_gate.fuzz import crowded

    for seed in range(10):
        ops = crowded(seed)
        late, by_year = ops[-1], sorted(ops, key=lambda o: o.year)
        i = by_year.index(late)
        assert [o.year for o in ops[:-1]] == sorted(
            o.year for o in ops[:-1]
        )  # rest arrive in order
        assert 0 < i < len(ops) - 1  # back-filled into the middle
        assert by_year[i - 1].wording == by_year[i + 1].wording == 2
        assert {o.wording for j, o in enumerate(by_year) if j not in (i - 1, i + 1)} == {0}
        assert len({o.value for o in ops}) == len(ops)
