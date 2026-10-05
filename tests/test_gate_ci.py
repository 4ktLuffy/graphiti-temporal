"""What CI relies on: exit codes, crashed workers, missing results, and confirmed fuzz findings."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from graphiti_gate import cli, fuzz

SCENARIOS = Path(cli.DEFAULT_SCENARIOS)


def _rec(**verdicts):
    return {'label': 'x', 'scenarios': {i: {'verdict': v} for i, v in verdicts.items()}}


def test_exit_code_fails_on_broke_error_and_conflict():
    assert cli.exit_code(_rec(a='pass', b='fail'), _rec(a='pass', b='pass')) == 0  # a fix
    assert cli.exit_code(_rec(a='pass'), _rec(a='fail')) == 1  # broke
    assert cli.exit_code(_rec(a='pass'), _rec(a='error')) == 1
    assert cli.exit_code(_rec(a='pass'), _rec()) == 1  # head lost a scenario
    assert cli.exit_code(_rec(a='pass'), {'conflict': 'x', 'scenarios': {}}) == 1


def _fake_worker(monkeypatch, tmp_path, stdout: str, returncode: int):
    rev = SimpleNamespace(python=Path('python'), label='fake')
    monkeypatch.setattr(cli, 'prepare', lambda spec, base='main': rev)
    monkeypatch.setattr(cli, 'RESULTS', tmp_path)
    done = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr='killed')
    monkeypatch.setattr(cli.subprocess, 'run', lambda *a, **k: done)


def test_a_worker_that_crashes_after_some_results_fails(monkeypatch, tmp_path):
    first = json.dumps({'id': 'anything', 'verdict': 'pass', 'passes': 1, 'repeats': 1})
    _fake_worker(monkeypatch, tmp_path, first + '\n', returncode=-9)
    with pytest.raises(RuntimeError, match='after 1 scenarios'):
        cli.run('main', SCENARIOS, 1)


def test_a_scenario_the_worker_never_reported_is_an_error(monkeypatch, tmp_path):
    _fake_worker(monkeypatch, tmp_path, '', returncode=0)
    record = cli.run('main', SCENARIOS, 1)
    assert record['scenarios']
    assert {r['verdict'] for r in record['scenarios'].values()} == {'error'}


def test_fuzz_keeps_only_findings_seen_twice(monkeypatch):
    stable = {'oracle': 'rule', 'backend': 'neo4j', 'detail': 'two current values'}
    once = {'oracle': 'concurrency', 'backend': 'neo4j', 'detail': {'run': 1}}

    async def again(ops, backends, concurrency, compare=True, via=False):
        return {'ops': [], 'findings': [stable]}

    monkeypatch.setattr(fuzz, 'trial_ops', again)
    result = {'seed': 1, 'ops': [], 'findings': [stable, once]}
    out = asyncio.run(fuzz.confirmed(result, ['neo4j'], True, False))
    assert out['findings'] == [stable]
    assert out['unconfirmed'] == [once]


def test_a_rerun_never_replaces_an_earlier_result(monkeypatch, tmp_path):
    line = json.dumps({'id': 'x', 'verdict': 'pass', 'passes': 1, 'repeats': 1})
    _fake_worker(monkeypatch, tmp_path, line + '\n', returncode=0)
    monkeypatch.setattr(cli, 'complete', lambda results, scenarios, repeats: results)
    cli.run('main', SCENARIOS, 1)
    cli.run('main', SCENARIOS, 1)
    names = sorted(p.name for p in tmp_path.glob('*.json'))
    assert len(names) == 2
    assert any(n.endswith('-run2.json') for n in names)
