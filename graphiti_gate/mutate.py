"""Which injected bugs does Graphiti's own test suite catch, and which does the gate catch?

    python -m graphiti_gate.mutate [--only ID ...] [--repeats 2] [--trials 30]

Graphiti's tests are the two jobs in `.github/workflows/unit_tests.yml`: the unit job (no
database), and the database job's Neo4j tests (`test_graphiti_mock.py`, `test_node_int.py`,
`test_edge_int.py`; FalkorDB is disabled here). They run in a dedicated checkout with
`uv sync --all-extras`, as CI does. A mutant is caught by them if a test that passes on the clean
base fails with the mutant.

The gate catches a mutant if any counted scenario (not `disputed`) changes verdict against the
base, or a random-history rule breaks in a history-size bucket where the base never breaks it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from graphiti_gate import cli
from graphiti_gate.mutants import HELD_OUT, MUTANTS, patch_for
from graphiti_gate.revisions import GATE, MIRROR, _git, ensure_mirror, prepare

UNIT_IGNORES = [
    'tests/test_graphiti_int.py', 'tests/test_graphiti_mock.py', 'tests/test_node_int.py',
    'tests/test_edge_int.py', 'tests/test_entity_exclusion_int.py', 'tests/driver/',
    'tests/llm_client/test_anthropic_client_int.py',
    'tests/utils/maintenance/test_temporal_operations_int.py',
    'tests/cross_encoder/test_bge_reranker_client_int.py', 'tests/evals/',
]  # fmt: skip
DB_TESTS = ['tests/test_graphiti_mock.py', 'tests/test_node_int.py', 'tests/test_edge_int.py']
OUT = cli.RESULTS / 'mutation'


def their_checkout(base: str = 'main') -> Path:
    ensure_mirror()
    sha = _git('rev-parse', f'origin/{base}') if base == 'main' else _git('rev-parse', base)
    path = GATE / 'unit' / sha[:12]
    if not (path / '.venv').exists():
        if not path.exists():
            _git('worktree', 'add', '--quiet', '--detach', str(path), sha, cwd=MIRROR)
        subprocess.run(['uv', 'sync', '--quiet', '--all-extras'], cwd=path, check=True)
    return path


def their_tests(path: Path, tag: str) -> dict[str, str]:
    """Outcome per test id ('passed' / 'failed' / 'skipped') over both CI jobs."""
    outcomes: dict[str, str] = {}
    env_common = {**os.environ, 'PYTHONPATH': str(path), 'DISABLE_NEPTUNE': '1'}
    jobs = [
        (['tests/', '-m', 'not integration', *[f'--ignore={i}' for i in UNIT_IGNORES]],
         {'DISABLE_NEO4J': '1', 'DISABLE_FALKORDB': '1', 'DISABLE_KUZU': '1'}),
        ([*DB_TESTS, '-m', 'not integration'], {'DISABLE_FALKORDB': '1', 'DISABLE_KUZU': '1',
         # A separate Neo4j, so their fixtures never touch a database the gate is using.
         'NEO4J_URI': os.environ.get('GATE_TESTS_NEO4J_URI', 'bolt://localhost:7688')}),
    ]  # fmt: skip
    for i, (args, env) in enumerate(jobs):
        xml = OUT / f'junit-{tag}-{i}.xml'
        xml.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ['uv', 'run', '--no-sync', 'pytest', '-q', '-p', 'no:cacheprovider',
             f'--junitxml={xml}', '--ignore=tests/embedder/test_voyage.py', *args],
            cwd=path, env={**env_common, **env}, capture_output=True, text=True, timeout=1800,
        )  # fmt: skip
        if not xml.exists():
            continue
        for case in ET.parse(xml).getroot().iter('testcase'):
            name = f'{case.get("classname")}::{case.get("name")}'
            if case.find('failure') is not None or case.find('error') is not None:
                outcomes[name] = 'failed'
            elif case.find('skipped') is not None:
                outcomes[name] = 'skipped'
            else:
                outcomes[name] = 'passed'
    return outcomes


def gate_verdicts(spec: str, repeats: int, trials: int) -> tuple[dict, dict]:
    run = cli.run(spec, cli.DEFAULT_SCENARIOS, repeats)
    props = cli.props(spec, trials, 25, seed=7)
    return run, props


def _holds(props: dict) -> dict[tuple[str, int], tuple[int, int]]:
    out = {}
    for rule in cli.RULES:
        for lo, hi in cli.BUCKETS:
            sub = [t for t in props['trials'] if lo <= t['homes'] <= hi]
            out[(rule, lo)] = (sum(t[rule] for t in sub), len(sub))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--repeats', type=int, default=2)
    ap.add_argument('--trials', type=int, default=30)
    ap.add_argument('--set', choices=['design', 'held-out', 'all'], default='design')
    args = ap.parse_args()
    pool = {'design': MUTANTS, 'held-out': HELD_OUT, 'all': MUTANTS + HELD_OUT}[args.set]
    OUT.mkdir(parents=True, exist_ok=True)

    path = their_checkout()
    base_tests = their_tests(path, 'base')
    base_run, base_props = gate_verdicts('main', args.repeats, args.trials)
    base_holds = _holds(base_props)
    print(json.dumps({'base': prepare('main').label,
                      'their_tests_passing_on_base': sum(v == 'passed' for v in base_tests.values())}),
          flush=True)  # fmt: skip

    for m in pool:
        if args.only and m.id not in args.only:
            continue
        patch = OUT / f'{m.id}.patch'
        patch.write_text(patch_for(m, prepare('main').worktree))

        subprocess.run(['git', 'apply', str(patch)], cwd=path, check=True)
        try:
            tests = their_tests(path, m.id)
        finally:
            subprocess.run(['git', 'checkout', '--', '.'], cwd=path, check=True)
        broken_tests = sorted(t for t, o in tests.items() if o == 'failed' and base_tests.get(t) == 'passed')  # fmt: skip

        run, props = gate_verdicts(f'patch:{patch}', args.repeats, args.trials)
        # Symmetric with their suite: a counted scenario that passes on the base must fail or error
        # with the mutant. Improvements and flaky outcomes are recorded, never counted.
        base_v = {
            i: r['verdict'] for i, r in base_run['scenarios'].items() if r['status'] != 'disputed'
        }
        mut_v = {i: r['verdict'] for i, r in run['scenarios'].items() if i in base_v}
        broke = sorted(
            i for i, v in mut_v.items() if base_v[i] == 'pass' and v in ('fail', 'error')
        )
        flaky = sorted(i for i, v in mut_v.items() if base_v[i] == 'pass' and v == 'flaky')
        improved = sorted(i for i, v in mut_v.items() if base_v[i] != 'pass' and v == 'pass')
        holds = _holds(props)
        rules_broken = sorted(
            f'{rule} ({lo}+ homes): {holds[(rule, lo)][0]}/{holds[(rule, lo)][1]}'
            for (rule, lo), (k, n) in base_holds.items()
            if n >= 5 and k == n and holds[(rule, lo)][1] and holds[(rule, lo)][0] < holds[(rule, lo)][1]
        )  # fmt: skip
        print(json.dumps({
            'mutant': m.id, 'what': m.what, 'held_out': m in HELD_OUT,
            'their_tests_caught': bool(broken_tests), 'their_failing_tests': broken_tests[:5],
            'gate_caught': bool(broke or rules_broken),
            'scenarios_broken': broke, 'rules_broken': rules_broken,
            'scenarios_flaky': flaky, 'scenarios_improved': improved,
            'verdicts': {'base': base_v, 'mutant': mut_v},
        }), flush=True)  # fmt: skip


if __name__ == '__main__':
    main()
