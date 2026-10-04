"""Runs scenarios against the Graphiti installed in the current interpreter, against Neo4j.

The gate starts this inside each revision's own virtualenv (`python -m graphiti_gate.worker`), so
the Graphiti under test is whatever that revision installed. Each repetition uses a fresh group.
Results are printed as JSON lines.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode

from graphiti_gate.compat import build_indices, make_clients
from graphiti_gate.judges import Claim, Judge
from graphiti_gate.scenario import FactSpec, Scenario, load
from graphiti_temporal.testing import HashEmbedder, NullCrossEncoder, hash_embedding
from graphiti_temporal.world import CITIES

UTC = timezone.utc


def _expand(s: Scenario) -> tuple[list[FactSpec], dict[str, str]]:
    """The scenario's facts plus generated moves; and fact id -> slot key 'subject/slot'."""
    facts = list(s.facts)
    if s.moves:
        m = s.moves
        cities = [c for c in CITIES if c not in {f.dst for f in s.facts + s.incoming}]
        for i in range(m.count):
            facts.append(
                FactSpec(
                    id=f'move{i}',
                    src=m.subject,
                    rel=m.rel,
                    dst=cities[i],
                    fact=m.template.format(subject=m.subject, city=cities[i]),
                    valid=datetime(m.start + 2 * i, 1, 1, tzinfo=UTC),
                    invalid=datetime(m.start + 2 * i + 2, 1, 1, tzinfo=UTC),
                    claim=(m.subject, m.slot, cities[i]),
                )  # fmt: skip
            )
    slots = {f.id: f'{f.claim[0]}/{f.claim[1]}' for f in facts + s.incoming if f.claim}
    return facts, slots


async def _node(driver, nodes: dict[str, EntityNode], name: str, group: str) -> EntityNode:
    if name not in nodes:
        node = EntityNode(
            name=name, group_id=group, labels=['Entity'], name_embedding=hash_embedding(name)
        )
        await node.save(driver)
        nodes[name] = node
    return nodes[name]


def _edge(spec: FactSpec, src: EntityNode, dst: EntityNode, group: str, now: datetime):
    invalid = spec.invalid_at()
    return EntityEdge(
        source_node_uuid=src.uuid, target_node_uuid=dst.uuid, group_id=group, name=spec.rel,
        fact=spec.fact, fact_embedding=hash_embedding(spec.fact),
        episodes=[str(uuid.uuid4()) for _ in range(spec.episodes)], created_at=now,
        valid_at=spec.valid_at(), invalid_at=invalid, expired_at=now if invalid else None,
    )  # fmt: skip


LIVE = os.environ.get('GATE_JUDGE') == 'live'


async def run_once(driver, s: Scenario, repetition: int = 0) -> dict[str, Any]:
    group = f'gate-{uuid.uuid4().hex[:12]}'
    now = datetime.now(UTC)
    facts, slots = _expand(s)
    judge_kwargs = dict(
        persona=s.judge,
        claims={f.fact: Claim(*f.claim) for f in facts + s.incoming if f.claim},
        script=s.script,
    )
    if LIVE:
        from graphiti_gate.live import LiveJudge

        judge = LiveJudge(**judge_kwargs, repetition=repetition)
    else:
        judge = Judge(**judge_kwargs)
    clients = make_clients(
        driver=driver, llm_client=judge, embedder=HashEmbedder(),
        cross_encoder=NullCrossEncoder(),
    )  # fmt: skip
    nodes: dict[str, EntityNode] = {}
    uuids: dict[str, str] = {}
    try:
        for f in facts:
            e = _edge(f, await _node(driver, nodes, f.src, group), await _node(driver, nodes, f.dst, group), group, now)  # fmt: skip
            await e.save(driver)
            uuids[f.id] = e.uuid

        if s.via in ('search', 'nodes', 'bulk_dedupe'):
            from graphiti_gate import vias

            if s.via == 'search':
                failures, extra = await vias.run_search(clients, s, group, uuids)
            elif s.via == 'nodes':
                failures, extra = await vias.run_nodes(clients, s, group, driver)
            else:
                for batch in s.bulk:
                    for f in batch:
                        await _node(driver, nodes, f['src'], group)
                        await _node(driver, nodes, f['dst'], group)
                failures, extra = await vias.run_bulk_dedupe(clients, s, group, nodes)
            return {'passed': not failures, 'failures': failures, 'state': extra,
                    'judge_calls': [c.__dict__ for c in judge.calls]}  # fmt: skip

        incoming = []
        for f in s.incoming:
            e = _edge(f, await _node(driver, nodes, f.src, group), await _node(driver, nodes, f.dst, group), group, now)  # fmt: skip
            e.expired_at = None  # set by Graphiti, not by the scenario
            incoming.append((f, e))
            uuids[f.id] = e.uuid
        merged: dict[str, str] = {}

        if s.via == 'resolve':
            from graphiti_core.utils.maintenance import edge_operations

            episode = EpisodicNode(
                name='gate', group_id=group, source=EpisodeType.text, source_description='gate',
                content='; '.join(f.fact for f, _ in incoming),
                valid_at=max((f.valid_at() for f, _ in incoming if f.valid_at()), default=now),
            )  # fmt: skip
            edges = [e for _, e in incoming]
            entities = list(nodes.values())
            resolved, invalidated, *_ = await edge_operations.resolve_extracted_edges(
                clients, edges, episode, entities, {}, {}
            )
            for (f, e), r in zip(incoming, resolved, strict=True):
                if r.uuid != e.uuid:
                    merged[f.id] = r.uuid
            for e in [*resolved, *invalidated]:
                if e.fact_embedding is None:
                    e.fact_embedding = hash_embedding(e.fact)
                await e.save(driver)
        else:
            from graphiti_core import Graphiti

            graphiti = Graphiti(
                graph_driver=driver, llm_client=judge, embedder=HashEmbedder(),
                cross_encoder=NullCrossEncoder(),
            )  # fmt: skip
            for f, e in incoming:
                result = await graphiti.add_triplet(nodes[f.src], e, nodes[f.dst])
                if result.edges and result.edges[0].uuid != e.uuid:
                    merged[f.id] = result.edges[0].uuid

        records, _, _ = await driver.execute_query(
            """
            MATCH ()-[e:RELATES_TO {group_id: $g}]->()
            RETURN e.uuid AS uuid, e.fact AS fact, e.valid_at AS valid_at,
                   e.invalid_at AS invalid_at, e.expired_at AS expired_at, e.episodes AS episodes
            """,
            g=group,
        )
        stored = {r['uuid']: r for r in records}
        return _evaluate(s, facts, slots, uuids, merged, stored, judge)
    finally:
        await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=group)


def _iso(value) -> str | None:
    if value is None:
        return None
    native = value.to_native() if hasattr(value, 'to_native') else value
    if native.tzinfo is None:
        native = native.replace(tzinfo=UTC)
    return native.astimezone(UTC).isoformat()


def _evaluate(s, facts, slots, uuids, merged, stored, judge) -> dict[str, Any]:  # noqa: C901
    state: dict[str, dict[str, Any]] = {}
    for fid, u in uuids.items():
        target = merged.get(fid, u)
        rec = stored.get(target)
        state[fid] = {
            'present': rec is not None,
            'merged_into': next((k for k, v in uuids.items() if v == merged.get(fid)), None)
            if fid in merged
            else None,
            'valid_at': _iso(rec['valid_at']) if rec else None,
            'invalid_at': _iso(rec['invalid_at']) if rec else None,
            'expired': rec is not None and rec['expired_at'] is not None,
            'episodes': len(rec['episodes'] or []) if rec else 0,
            'fact': rec['fact'] if rec else None,
        }
    failures = []
    for fid, exp in s.expect.items():
        st = state[fid]
        if exp.merged is not None and (st['merged_into'] is not None) != exp.merged:
            failures.append(
                f'{fid}: expected {"" if exp.merged else "not "}merged, got {_desc(st)}'
            )
        if exp.episodes is not None and st['episodes'] != exp.episodes:
            failures.append(f'{fid}: expected {exp.episodes} episodes, got {st["episodes"]}')
        if exp.merged_into is not None and st['merged_into'] != exp.merged_into:
            failures.append(f'{fid}: expected merged into {exp.merged_into}, got {_desc(st)}')
        is_open = st['present'] and st['invalid_at'] is None and not st['expired']
        if exp.open is not None and is_open != exp.open:
            failures.append(f'{fid}: expected {"open" if exp.open else "closed"}, got {_desc(st)}')
        if exp.invalid_at is not None:
            want = _iso(FactSpec(id='x', src='', rel='', dst='', fact='', invalid=exp.invalid_at).invalid_at())  # fmt: skip
            if st['invalid_at'] != want:
                failures.append(f'{fid}: expected invalid_at {want[:10]}, got {_desc(st)}')
        if exp.expired is not None and st['expired'] != exp.expired:
            failures.append(f'{fid}: expected expired={exp.expired}, got {_desc(st)}')
    # The whole stored graph is checked, not only the facts the scenario names.
    known = set(uuids.values())
    for u, rec in stored.items():
        if u not in known:
            failures.append(f'unexpected stored edge: "{rec["fact"]}"')
    for fid in merged:
        if uuids[fid] in stored:
            failures.append(
                f'{fid}: merged into {state[fid]["merged_into"]} but also stored itself'
            )
    # A fact the scenario does not mention must keep its text, interval, expiry and provenance;
    # a fact that absorbed merged duplicates may gain exactly their episodes.
    absorbed: dict[str, int] = {}
    for fid in merged:
        target = state[fid]['merged_into']
        if target:
            absorbed[target] = absorbed.get(target, 0) + 1
    for spec in facts:
        fid = spec.id
        if fid in s.expect:
            continue
        st = state[fid]
        if not st['present']:
            failures.append(f'{fid}: unexpectedly deleted')
            continue
        was = {
            'fact': spec.fact,
            'valid_at': _iso(spec.valid_at()),
            'invalid_at': _iso(spec.invalid_at()),
            'expired': spec.invalid_at() is not None,
            'episodes': spec.episodes + absorbed.get(fid, 0),
        }
        for key, before in was.items():
            if st[key] != before:
                failures.append(f'{fid}: unexpectedly changed {key}: {before} -> {st[key]}')
    for slot, want_ids in s.open_in_slot.items():
        open_ids = sorted(
            fid
            for fid, st in state.items()
            if slots.get(fid) == slot and st['present'] and st['merged_into'] is None
            and st['invalid_at'] is None and not st['expired']
        )  # fmt: skip
        if open_ids != sorted(want_ids):
            failures.append(f'{slot}: expected open {sorted(want_ids)}, got {open_ids}')
    live = {'cache_hits': judge.hits, 'cache_misses': judge.misses} if LIVE else {}
    return {
        **live,
        'passed': not failures,
        'failures': failures,
        'state': {k: v for k, v in state.items() if not k.startswith('move') or not v['expired']},
        'judge_calls': [c.__dict__ for c in judge.calls],
    }


def _desc(st: dict[str, Any]) -> str:
    if not st['present']:
        return 'missing'
    if st['merged_into']:
        return f'merged into {st["merged_into"]}'
    end = st['invalid_at'][:10] if st['invalid_at'] else 'open'
    return f'{end}{", expired" if st["expired"] else ""}'


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    logging.getLogger('graphiti_core').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('paths', nargs='+', type=Path)
    ap.add_argument('--repeats', type=int, default=5)
    args = ap.parse_args()
    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    await build_indices(driver)

    from graphiti_temporal.codex_llm import CodexQuotaExhausted

    slots = asyncio.Semaphore(int(os.environ.get('GATE_LIVE_PARALLEL', '4')) if LIVE else 1)

    async def attempt(s: Scenario, rep: int) -> dict[str, Any]:
        async with slots:
            try:
                return await run_once(driver, s, rep)
            except CodexQuotaExhausted:
                raise
            except Exception as e:  # recorded per scenario; one broken revision path is a result
                return {'passed': False, 'error': f'{type(e).__name__}: {e}'[:400],
                        'trace': traceback.format_exc()[-1500:]}  # fmt: skip

    for s in load(args.paths):
        try:
            runs = await asyncio.gather(*(attempt(s, rep) for rep in range(args.repeats)))
        except CodexQuotaExhausted as e:
            print(json.dumps({'quota_exhausted': str(e)[:300]}), flush=True)
            await driver.close()
            return 3
        verdicts = {r['passed'] for r in runs}
        errored = any('error' in r for r in runs)
        verdict = 'error' if errored else ('flaky' if len(verdicts) > 1 else ('pass' if True in verdicts else 'fail'))  # fmt: skip
        print(json.dumps({
            'id': s.id, 'family': s.family, 'status': s.status, 'judge': s.judge,
            'title': s.title, 'source': s.source, 'verdict': verdict,
            'passes': sum(r['passed'] for r in runs), 'repeats': len(runs),
            'first': runs[0],
            'live': LIVE, 'runs': [{k: r.get(k) for k in ('passed', 'failures', 'error', 'judge_calls', 'cache_misses')} for r in runs] if LIVE else None,
        }), flush=True)  # fmt: skip
    await driver.close()


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
