"""Random workloads, three oracles, minimal cases: Graphiti checked without hand-written answers.

The fuzzer generates small random histories (people changing homes and employers, facts arriving
out of order, through `resolve_extracted_edges` or `add_triplet`), runs them through Graphiti's
real code with the oracle judge, and checks the result three ways, none of which needs a
hand-written expected outcome:

1. **Rules.** At most one current value per person and slot; every value ends exactly where the
   next one starts.
2. **A Python reference for search.** "As of T" searches must return exactly the facts the stored
   graph says were valid at T. The reference is computed from the stored state itself, so each
   backend is checked independently.
3. **Backends against each other.** Neo4j, FalkorDB and Kuzu, given the same workload, must store
   the same facts with the same intervals.

Workloads are kept below the 10-candidate search limit, so ranking differences between backends
cannot change Graphiti's decisions. Every finding must reproduce on a second run before it is
reported. Runs inside a revision's virtualenv:

    python -m graphiti_gate.fuzz --trials 50 --backends neo4j falkordb kuzu
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import uuid
import warnings
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from typing import Any

from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from graphiti_gate.compat import build_indices, make_clients
from graphiti_gate.judges import Claim, Judge
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, hash_embedding
from graphiti_temporal.world import CITIES

UTC = timezone.utc
SUBJECTS = ['Alice', 'Bruno', 'Chen']
SLOTS = {
    'home': ('LIVES_IN', '{s} lives in {v}', ['Paris', 'Rome', 'Oslo', 'Lima', 'Cairo', 'Seoul', 'Quito', 'Dublin']),
    'employer': ('WORKS_AT', '{s} works at {v}', ['Acme', 'Globex', 'Initech', 'Umbrella', 'Hooli', 'Vandelay']),
}  # fmt: skip
# The same claim in other words, which sends Graphiti down its duplicate path.
REWORDED = {'home': '{s} resides in {v}', 'employer': '{s} is employed by {v}'}
# More wordings for `--varied`: ranked search then misses a fact's true neighbours more often.
WORDINGS = {
    'home': ['{s} has made a home in {v}', '{s} rents a flat in {v}', '{s} is based in {v}'],
    'employer': [
        '{s} has a job at {v}',
        '{s} draws a salary from {v}',
        '{s} is on the payroll of {v}',
    ],
}


@dataclass(frozen=True)
class Op:
    via: str  # 'resolve' or 'triplet'
    subject: str
    slot: str
    value: str
    year: int
    reworded: bool = False
    wording: int = 0  # 1-3 picks from WORDINGS (`--varied`); 0 keeps the two above

    @property
    def fact(self) -> str:
        if self.wording:
            template = WORDINGS[self.slot][self.wording - 1]
        else:
            template = REWORDED[self.slot] if self.reworded else SLOTS[self.slot][1]
        return template.format(s=self.subject, v=self.value)


def crowded(seed: int) -> list[Op]:
    """One person's homes in date order, then one home back-filled into the middle.

    The back-filled home's two true neighbours, the home before it and the home after it, are
    worded differently from every other home, so ranked search prefers the rest and can leave
    both out of the 10 candidate places. A correct Graphiti ends the back-filled home where the
    next begins and shortens the one before. Long histories: databases are not compared.
    """
    rng = random.Random(f'crowded-{seed}')
    years = sorted(rng.sample(range(1950, 2025), rng.randint(14, 30)))
    late = rng.randrange(2, len(years) - 2)
    homes = rng.sample(CITIES, len(years))  # distinct, so no home is a duplicate of another
    ops = [Op(rng.choice(['resolve', 'triplet']), 'Alice', 'home', homes[i], y,
              wording=2 if i in (late - 1, late + 1) else 0)
           for i, y in enumerate(years)]  # fmt: skip
    return [*ops[:late], *ops[late + 1 :], ops[late]]


def workload(seed: int, max_ops: int = 8, deep: bool = False, varied: bool = False) -> list[Op]:
    """Random ops; distinct years per person and slot (equal starts are a documented no-op).

    `deep` makes long histories for one to three people (12 to 30 ops), past the 10-candidate
    search limit, where ranking decides what Graphiti sees. Backend differences are then
    expected and only the rule and search oracles are meaningful. `varied` words facts five ways
    instead of two; workloads without it are unchanged for every seed.
    """
    rng = random.Random(seed)
    ops, used = [], set()
    subjects = SUBJECTS[: rng.randint(1, 3)] if deep else SUBJECTS[:2]
    for _ in range(rng.randint(12, 30) if deep else rng.randint(2, max_ops)):
        subject, slot = rng.choice(subjects), rng.choice(list(SLOTS))
        year = rng.choice([y for y in range(1990, 2025) if (subject, slot, y) not in used])
        used.add((subject, slot, year))
        value = rng.choice(SLOTS[slot][2])
        ops.append(Op(rng.choice(['resolve', 'resolve', 'triplet']), subject, slot, value, year,
                      reworded=rng.random() < 0.25))  # fmt: skip
    if varied:
        wordings = random.Random(f'{seed}-wording')
        ops = [replace(o, wording=wordings.randrange(4)) for o in ops]
    return ops


# --- backends -------------------------------------------------------------------------------


async def open_backend(name: str, group: str):
    if name == 'neo4j':
        from graphiti_core.driver.neo4j_driver import Neo4jDriver

        driver = Neo4jDriver(os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
                             os.environ.get('NEO4J_USER', 'neo4j'), os.environ['NEO4J_PASSWORD'])  # fmt: skip
    elif name == 'falkordb':
        from falkordb.asyncio import FalkorDB
        from graphiti_core.driver.falkordb_driver import FalkorDriver

        port = int(os.environ.get('FALKORDB_PORT', '6380'))
        driver = FalkorDriver(falkor_db=FalkorDB(host='localhost', port=port), database=group)
        await asyncio.sleep(0.5)  # the driver builds indices in the background on creation
    elif name == 'kuzu':
        from graphiti_core.driver.kuzu_driver import KuzuDriver

        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            driver = KuzuDriver(':memory:')
        # Kuzu's build_indices_and_constraints is a no-op, so fulltext indices are never created
        # and every fulltext search fails (Kuzu is deprecated; Graphiti's own tests skip it). The
        # fuzzer creates them from Graphiti's own statements so Kuzu can still be compared.
        from graphiti_core.driver.driver import GraphProvider
        from graphiti_core.graph_queries import get_fulltext_indices

        await driver.execute_query('INSTALL fts')
        await driver.execute_query('LOAD EXTENSION fts')
        for q in get_fulltext_indices(GraphProvider.KUZU):
            await driver.execute_query(q)
    else:
        raise ValueError(name)
    await build_indices(driver)
    return driver


async def close_backend(name: str, driver, group: str) -> None:
    try:
        if name == 'falkordb':
            await driver.client.select_graph(group).delete()
        elif name == 'neo4j':
            await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)
    finally:
        await driver.close()


# --- running a workload ---------------------------------------------------------------------


async def run(ops: list[Op], backend: str, concurrent: bool = False) -> dict[str, Any]:
    group = f'fz{uuid.uuid4().hex[:10]}'
    driver = await open_backend(backend, group)
    judge = Judge(persona='oracle', claims={o.fact: Claim(o.subject, o.slot, o.value) for o in ops})
    clients = make_clients(driver=driver, llm_client=judge, embedder=HashEmbedder(),
                           cross_encoder=NullCrossEncoder())  # fmt: skip
    nodes: dict[str, EntityNode] = {}
    now = datetime.now(UTC)

    async def node(name: str) -> EntityNode:
        if name not in nodes:
            nodes[name] = EntityNode(name=name, group_id=group, labels=['Entity'],
                                     name_embedding=hash_embedding(name))  # fmt: skip
            await nodes[name].save(driver)
        return nodes[name]

    async def apply(op: Op) -> None:
        start = datetime(op.year, 1, 1, tzinfo=UTC)
        src, dst = await node(op.subject), await node(op.value)
        edge = EntityEdge(source_node_uuid=src.uuid, target_node_uuid=dst.uuid, group_id=group,
                          name=SLOTS[op.slot][0], fact=op.fact, fact_embedding=hash_embedding(op.fact),
                          episodes=[], created_at=now, valid_at=start)  # fmt: skip
        if op.via == 'triplet':
            from graphiti_core import Graphiti

            graphiti = Graphiti(graph_driver=driver, llm_client=judge, embedder=HashEmbedder(),
                                cross_encoder=NullCrossEncoder())  # fmt: skip
            await graphiti.add_triplet(src, edge, dst)
            return
        from graphiti_core.utils.maintenance import edge_operations

        episode = EpisodicNode(name='fz', group_id=group, source=EpisodeType.text,
                               source_description='fuzz', content=op.fact, valid_at=start)  # fmt: skip
        resolved, invalidated, *_ = await edge_operations.resolve_extracted_edges(
            clients, [edge], episode, list(nodes.values()), {}, {}
        )
        for e in [*resolved, *invalidated]:
            e.fact_embedding = e.fact_embedding or hash_embedding(e.fact)
            await e.save(driver)

    error = None
    try:
        for name in sorted({o.subject for o in ops} | {o.value for o in ops}):
            await node(name)  # create entities up front so concurrent ops do not race on them
        if concurrent:
            await asyncio.gather(*(apply(o) for o in ops))
        else:
            for o in ops:
                await apply(o)
        state = await snapshot(driver, group)
        probes = await probe(clients, group, ops, state)
    except Exception as e:  # a crash is a finding, not a harness failure
        error = f'{type(e).__name__}: {str(e)[:300]}'
        state, probes = [], {}
    finally:
        await close_backend(backend, driver, group)
    return {
        'backend': backend,
        'concurrent': concurrent,
        'error': error,
        'state': state,
        'probes': probes,
    }


async def snapshot(driver, group: str) -> list[tuple]:
    """The stored facts, canonical across backends: endpoints by name, dates as ISO strings."""
    edges = await EntityEdge.get_by_group_ids(driver, [group])
    names = {n.uuid: n.name for n in await EntityNode.get_by_group_ids(driver, [group])}

    def iso(d):
        if d is None:
            return None
        d = d if isinstance(d, datetime) else datetime.fromisoformat(str(d))
        return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC).date().isoformat()

    return sorted(
        (names.get(e.source_node_uuid, '?'), names.get(e.target_node_uuid, '?'), e.fact,
         iso(e.valid_at), iso(e.invalid_at), e.expired_at is not None)
        for e in edges
    )  # fmt: skip


async def probe(clients, group: str, ops: list[Op], state: list[tuple]) -> dict[str, Any]:
    """As-of searches against a reference computed from the stored state itself."""
    from graphiti_core.search import search as search_module
    from graphiti_core.search.search_config import (
        EdgeReranker,
        EdgeSearchConfig,
        EdgeSearchMethod,
        SearchConfig,
    )
    from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters

    out = {}
    years = sorted({o.year for o in ops})
    checkpoints = sorted({years[0], years[-1] + 1, (years[0] + years[-1]) // 2})
    for subject in sorted({o.subject for o in ops}):
        for y in checkpoints:
            t = datetime(y, 6, 1, tzinfo=UTC)
            iso_t = t.date().isoformat()
            want = sorted(
                f for (s, _d, f, v, i, _x) in state
                if s == subject and v is not None and v <= iso_t and (i is None or i > iso_t)
            )  # fmt: skip
            flt = SearchFilters(
                valid_at=[[DateFilter(date=t, comparison_operator=ComparisonOperator.less_than_equal)]],
                invalid_at=[[DateFilter(comparison_operator=ComparisonOperator.is_null)],
                            [DateFilter(date=t, comparison_operator=ComparisonOperator.greater_than)]],
            )  # fmt: skip
            config = SearchConfig(edge_config=EdgeSearchConfig(
                search_methods=[EdgeSearchMethod.bm25], reranker=EdgeReranker.rrf), limit=100)  # fmt: skip
            res = await search_module.search(clients, subject, [group], config, flt)
            got = sorted(e.fact for e in res.edges if e.fact.startswith(subject + ' '))
            out[f'{subject}@{y}'] = {'want': want, 'got': got}
        # Facts that started before the first checkpoint OR after the last: two OR groups.
        lo, hi = (
            datetime(checkpoints[0], 1, 1, tzinfo=UTC),
            datetime(checkpoints[-1], 1, 1, tzinfo=UTC),
        )
        want = sorted(
            f for (s, _d, f, v, _i, _x) in state
            if s == subject and v is not None and (v < lo.date().isoformat() or v > hi.date().isoformat())
        )  # fmt: skip
        flt = SearchFilters(valid_at=[
            [DateFilter(date=lo, comparison_operator=ComparisonOperator.less_than)],
            [DateFilter(date=hi, comparison_operator=ComparisonOperator.greater_than)],
        ])  # fmt: skip
        res = await search_module.search(clients, subject, [group], config, flt)
        got = sorted(e.fact for e in res.edges if e.fact.startswith(subject + ' '))
        out[f'{subject} started before {lo.year} or after {hi.year}'] = {'want': want, 'got': got}
    return out


# --- oracles ---------------------------------------------------------------------------------


def rule_findings(ops: list[Op], result: dict[str, Any]) -> list[str]:
    if result['error']:
        return [f'crash: {result["error"]}']
    found = []
    by_slot: dict[tuple, list[tuple]] = {}
    slot_of = {SLOTS[o.slot][0]: o.slot for o in ops}
    rel_of = {o.fact: o.slot for o in ops}
    for s, _d, f, v, i, x in result['state']:
        by_slot.setdefault((s, rel_of.get(f, slot_of.get('?'))), []).append((v, i, x, f))
    for s, _d, f, v, i, _x in result['state']:
        if v is not None and i is not None and i <= v:
            found.append(f'{s}: "{f}" has an empty or inverted interval {v} to {i}')
    for (s, slot), facts in sorted(by_slot.items(), key=str):
        open_now = [f for (_v, i, x, f) in facts if i is None and not x]
        if len(open_now) > 1:
            found.append(f'{s}/{slot}: {len(open_now)} current values {sorted(open_now)}')
        chain = sorted(facts)
        for (v1, i1, _x1, f1), (v2, _i2, _x2, _f2) in zip(chain, chain[1:], strict=False):
            if v1 != v2 and i1 != v2:
                found.append(f'{s}/{slot}: "{f1}" ends {i1}, next starts {v2}')
    for key, p in sorted(result['probes'].items()):
        if p['want'] != p['got']:
            found.append(f'search as of {key}: got {p["got"]}, stored state says {p["want"]}')
    return found


async def trial(
    seed: int,
    backends: list[str],
    concurrency: bool,
    deep: bool = False,
    varied: bool = False,
    crowd: bool = False,
    via: bool = False,
) -> dict[str, Any]:
    ops = crowded(seed) if crowd else workload(seed, deep=deep, varied=varied)
    result = await trial_ops(ops, backends, concurrency, compare=not (deep or crowd), via=via)
    return {'seed': seed, **result}


async def confirmed(
    result: dict[str, Any], backends, concurrency, deep, via=False
) -> dict[str, Any]:
    """Rerun a workload with findings; only findings seen on both runs stay in `findings`."""
    if not result['findings']:
        return result
    ops = [Op(**o) for o in result['ops']]
    again = await trial_ops(ops, backends, concurrency, compare=not deep, via=via)
    seen = {json.dumps(f, sort_keys=True, default=str) for f in again['findings']}
    keep = [f for f in result['findings'] if json.dumps(f, sort_keys=True, default=str) in seen]
    gone = [f for f in result['findings'] if f not in keep]
    return {**result, 'findings': keep, 'unconfirmed': gone}


async def via_findings(ops: list[Op], backend: str) -> list[dict[str, Any]]:
    """The same facts through `resolve_extracted_edges` only and through `add_triplet` only.

    With a correct judge both ingestion paths must store the same facts with the same dates.
    """
    paths = {
        via: await run([replace(o, via=via) for o in ops], backend)
        for via in ('resolve', 'triplet')
    }
    if (
        any(r['error'] for r in paths.values())
        or paths['resolve']['state'] == paths['triplet']['state']
    ):
        return []
    a, b = (set(map(tuple, paths[v]['state'])) for v in ('resolve', 'triplet'))
    return [{'oracle': 'via', 'backend': backend,
             'detail': {'resolve_only': sorted(a - b), 'triplet_only': sorted(b - a)}}]  # fmt: skip


async def trial_ops(
    ops: list[Op], backends: list[str], concurrency: bool, compare: bool = True, via: bool = False
) -> dict[str, Any]:
    results = {b: await run(ops, b) for b in backends}
    findings = []
    for b, r in results.items():
        findings += [{'oracle': 'rule', 'backend': b, 'detail': d} for d in rule_findings(ops, r)]
    states = {b: r['state'] for b, r in results.items() if not r['error']}
    if compare and len(set(map(json.dumps, states.values()))) > 1:
        ref = backends[0]
        for b, st in states.items():
            if st != states.get(ref):
                only_b = sorted(set(map(tuple, st)) - set(map(tuple, states[ref])))
                only_ref = sorted(set(map(tuple, states[ref])) - set(map(tuple, st)))
                findings.append({'oracle': 'backends', 'backend': f'{b} vs {ref}',
                                 'detail': {'only_' + b: only_b, 'only_' + ref: only_ref}})  # fmt: skip
    if via:
        findings += await via_findings(ops, backends[0])
    if concurrency:
        seq = results.get('neo4j') or await run(ops, 'neo4j')
        par = await run(ops, 'neo4j', concurrent=True)
        if not par['error'] and par['state'] != seq['state']:
            findings.append({'oracle': 'concurrency', 'backend': 'neo4j',
                             'detail': {'sequential_only': sorted(set(map(tuple, seq['state'])) - set(map(tuple, par['state']))),
                                        'concurrent_only': sorted(set(map(tuple, par['state'])) - set(map(tuple, seq['state'])))}})  # fmt: skip
        elif par['error']:
            findings.append(
                {'oracle': 'concurrency', 'backend': 'neo4j', 'detail': f'crash: {par["error"]}'}
            )
    return {'ops': [asdict(o) for o in ops], 'findings': findings}


async def main() -> None:
    logging.disable(logging.CRITICAL)
    ap = argparse.ArgumentParser()
    ap.add_argument('--trials', type=int, default=30)
    ap.add_argument('--first-seed', type=int, default=0)
    ap.add_argument('--backends', nargs='+', default=['neo4j', 'falkordb', 'kuzu'])
    ap.add_argument('--concurrency', action='store_true')
    ap.add_argument('--deep', action='store_true', help='histories past the candidate limit')
    ap.add_argument('--varied', action='store_true', help='word each fact one of five ways')
    ap.add_argument('--crowded', action='store_true', help='back-fill one home into a long history')
    ap.add_argument('--via', action='store_true', help='compare add_episode and add_triplet paths')
    args = ap.parse_args()
    for seed in range(args.first_seed, args.first_seed + args.trials):
        result = await trial(
            seed, args.backends, args.concurrency, args.deep, args.varied, args.crowded, args.via
        )
        result = await confirmed(
            result, args.backends, args.concurrency, args.deep or args.crowded, args.via
        )
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
