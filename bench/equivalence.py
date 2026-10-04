"""Is a surviving mutant equivalent, i.e. unable to change any outcome? Checked, not argued.

Runs Graphiti's real `resolve_extracted_edge` and a mutated copy of it on random histories whose
dates sit on a coarse grid, so equal-timestamp boundaries are frequent, and compares the full
result: resolved edge, its dates and expiry, the retired facts with their end dates and expiry.

    uv run python bench/equivalence.py skip-ended-boundary skip-new-ended-boundary --cases 20000
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.utils.maintenance import edge_operations

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'tests'))
from test_lossless import ScriptedModel  # noqa: E402

from graphiti_gate.mutants import HELD_OUT, MUTANTS  # noqa: E402

UTC = timezone.utc
BASE = datetime(2000, 1, 1, tzinfo=UTC)


def mutated_module(mutant_id: str):
    m = next(x for x in MUTANTS + HELD_OUT if x.id == mutant_id)
    source = inspect.getsource(edge_operations)
    assert source.count(m.old) == 1, m.id
    namespace: dict = {'__name__': f'mutant_{m.id.replace("-", "_")}'}
    exec(compile(source.replace(m.old, m.new), f'<mutant {m.id}>', 'exec'), namespace)
    return namespace


def _day(rng: random.Random) -> datetime | None:
    return None if rng.random() < 0.1 else BASE + timedelta(days=10 * rng.randrange(0, 6))


def _edge(name: str, valid, invalid, expired: bool | None = None) -> EntityEdge:
    return EntityEdge(
        source_node_uuid='s', target_node_uuid=f't-{name}', group_id='g', name='R',
        fact=f'fact {name}', episodes=[f'ep-{name}'], created_at=BASE, valid_at=valid,
        invalid_at=invalid, expired_at=BASE if (invalid if expired is None else expired) else None,
    )  # fmt: skip


def _case(rng: random.Random):
    def interval(name):
        start, end = _day(rng), (None if rng.random() < 0.4 else _day(rng))
        if start and end and end < start:
            start, end = end, start
        # Ended but never stamped expired is a real state (#1865), so it is generated too.
        return _edge(name, start, end, expired=bool(end) and rng.random() < 0.7)

    new_start, new_end = _day(rng), (None if rng.random() < 0.6 else _day(rng))
    if new_start and new_end and new_end < new_start:
        new_start, new_end = new_end, new_start  # well-formed, like every other interval here
    new = _edge('new', new_start, new_end)
    new.expired_at = None
    related = [interval(f'd{i}') for i in range(rng.randrange(0, 3))]
    candidates = [interval(f'c{i}') for i in range(rng.randrange(1, 6))]
    duplicates = {e.fact for e in related if rng.random() < 0.5}
    contradicted = {e.fact for e in related + candidates if rng.random() < 0.8}
    return new, related, candidates, duplicates, contradicted


def _summary(resolved, invalidated):
    return (
        resolved.fact, resolved.valid_at, resolved.invalid_at, resolved.expired_at is not None,
        sorted((e.fact, e.invalid_at, e.expired_at is not None) for e in invalidated),
    )  # fmt: skip


def _stored(new, related, candidates, resolved, invalidated) -> list:
    """What Graphiti would store, per edge id: it saves the resolved and invalidated edges; the
    new edge exists only if it was not merged into a duplicate."""
    edges = {e.uuid: e for e in [*related, *candidates]}
    if resolved.uuid == new.uuid:
        edges[new.uuid] = new
    edges.update({e.uuid: e for e in [resolved, *invalidated]})
    return sorted(
        (u, e.fact, e.source_node_uuid, e.target_node_uuid, e.valid_at, e.invalid_at,
         e.expired_at is not None, tuple(sorted(e.episodes)))
        for u, e in edges.items()
    )  # fmt: skip


async def compare(fn_a, fn_b, cases: int, seed: int) -> list:
    differences = []
    episode = EpisodicNode(
        name='e', group_id='g', source=EpisodeType.text, source_description='', content='',
        valid_at=BASE,
    )  # fmt: skip
    for i in range(cases):
        rng = random.Random(seed * 1_000_003 + i)
        new, related, candidates, dups, contra = _case(rng)
        outs, states = [], []
        for fn in (fn_a, fn_b):
            new_c = new.model_copy(deep=True)
            rel_c = [e.model_copy(deep=True) for e in related]
            cand_c = [e.model_copy(deep=True) for e in candidates]
            r, inv, _ = await fn(ScriptedModel(dups, contra, None), new_c, rel_c, cand_c, episode)
            outs.append(_summary(r, inv))
            states.append(_stored(new_c, rel_c, cand_c, r, inv))
        if outs[0] != outs[1] or states[0] != states[1]:
            differences.append((i, outs, states[0] != states[1]))
    return differences


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('mutants', nargs='+')
    ap.add_argument('--cases', type=int, default=20000)
    args = ap.parse_args()
    for mutant_id in args.mutants:
        mutated = mutated_module(mutant_id)['resolve_extracted_edge']
        diffs = await compare(edge_operations.resolve_extracted_edge, mutated, args.cases, seed=1)
        stored = [d for d in diffs if d[2]]
        print(
            f'{mutant_id}: result differs in {len(diffs)} of {args.cases} cases; '
            f'stored graph differs in {len(stored)}',
            flush=True,
        )
        for i, (a, b), _ in stored[:1]:
            print(f'  case {i}\n    original {a}\n    mutant   {b}')


if __name__ == '__main__':
    asyncio.run(main())
