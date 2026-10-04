"""Audit a live Graphiti graph for the damage the faults in FINDINGS.md leave behind. Read-only.

    graphiti-gate audit [--group G ...] [--exposure 9] [--json]

Checks, per group:

- **two current values**: a subject with more than one open fact under the same relation name.
  Graphiti does not record whether a relation can hold several values (#1728's additive case),
  so these are candidates for review, ranked; nothing is changed.
- **exposure**: a subject with `--exposure` or more superseded facts under one relation. From
  there on, Graphiti's 10-candidate invalidation search can miss the live fact (F1).
- **inverted intervals**: `invalid_at` earlier than `valid_at`. Graphiti never validates intervals.
- **zero-length intervals**: `invalid_at` equal to `valid_at`.
- **ended but not expired**: `invalid_at` set, `expired_at` empty (#1865's finalisation gap).

For every two-current-values candidate whose facts carry start dates, the report prints the
Cypher that would end the older fact at the newer one's start. It never runs it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from graphiti_core.driver.neo4j_driver import Neo4jDriver

OPEN = 'e.invalid_at IS NULL AND e.expired_at IS NULL'


async def _q(driver, cypher: str, **params) -> list[dict[str, Any]]:
    records, _, _ = await driver.execute_query(cypher, routing_='r', **params)
    return [dict(r) for r in records]


def _iso(v) -> str | None:
    if v is None:
        return None
    native = v.to_native() if hasattr(v, 'to_native') else v
    return native.isoformat()


async def audit(driver, groups: list[str] | None, exposure: int) -> dict[str, Any]:
    where_group = 'AND e.group_id IN $groups' if groups else ''
    params = {'groups': groups} if groups else {}
    double = await _q(
        driver,
        f"""
        MATCH (s:Entity)-[e:RELATES_TO]->(t:Entity)
        WHERE {OPEN} {where_group}
        WITH e.group_id AS group_id, s, e.name AS relation,
             collect({{uuid: e.uuid, fact: e.fact, valid_at: e.valid_at, target: t.name}}) AS facts
        WHERE size(facts) > 1
        RETURN group_id, s.uuid AS subject_uuid, s.name AS subject, relation, facts
        ORDER BY size(facts) DESC
        """,
        **params,
    )
    exposed = await _q(
        driver,
        f"""
        MATCH (s:Entity)-[e:RELATES_TO]->()
        WHERE e.invalid_at IS NOT NULL {where_group}
        WITH e.group_id AS group_id, s, e.name AS relation, count(e) AS superseded
        WHERE superseded >= $exposure
        RETURN group_id, s.name AS subject, relation, superseded
        ORDER BY superseded DESC
        """,
        exposure=exposure,
        **params,
    )
    interval = await _q(
        driver,
        f"""
        MATCH (s:Entity)-[e:RELATES_TO]->()
        WHERE e.invalid_at IS NOT NULL {where_group}
          AND (e.valid_at IS NOT NULL AND e.invalid_at <= e.valid_at OR e.expired_at IS NULL)
        RETURN e.group_id AS group_id, e.uuid AS uuid, e.fact AS fact, e.valid_at AS valid_at,
               e.invalid_at AS invalid_at, e.expired_at IS NULL AS not_expired
        """,
        **params,
    )
    totals = (
        await _q(
            driver,
            f"""
            MATCH ()-[e:RELATES_TO]->() WHERE true {where_group}
            RETURN count(e) AS facts, sum(CASE WHEN e.invalid_at IS NULL THEN 1 ELSE 0 END) AS open
            """,
            **params,
        )
    )[0]

    inverted = [
        r for r in interval if r['valid_at'] is not None and r['invalid_at'] < r['valid_at']
    ]
    zero = [r for r in interval if r['valid_at'] is not None and r['invalid_at'] == r['valid_at']]
    unexpired = [r for r in interval if r['not_expired']]
    repairs = []
    for d in double:
        dated = sorted(
            (f for f in d['facts'] if f['valid_at'] is not None), key=lambda f: f['valid_at']
        )
        for older, newer in zip(dated, dated[1:], strict=False):  # consecutive pairs
            repairs.append({
                'group_id': d['group_id'], 'subject': d['subject'], 'relation': d['relation'],
                'end': older['fact'], 'at': _iso(newer['valid_at']), 'because': newer['fact'],
                'cypher': (f"MATCH ()-[e:RELATES_TO {{uuid: '{older['uuid']}'}}]->() "
                           f"SET e.invalid_at = datetime('{_iso(newer['valid_at'])}'), "
                           'e.expired_at = datetime()'),
            })  # fmt: skip
    return {
        'facts': totals['facts'],
        'open_facts': totals['open'],
        'two_current_values': [
            {
                **{k: d[k] for k in ('group_id', 'subject', 'relation')},
                'open': [{'fact': f['fact'], 'valid_at': _iso(f['valid_at'])} for f in d['facts']],
            }
            for d in double
        ],  # fmt: skip
        'exposed': exposed,
        'inverted': [
            {
                'fact': r['fact'],
                'valid_at': _iso(r['valid_at']),
                'invalid_at': _iso(r['invalid_at']),
            }
            for r in inverted
        ],  # fmt: skip
        'zero_length': [{'fact': r['fact'], 'at': _iso(r['valid_at'])} for r in zero],
        'ended_not_expired': [
            {'fact': r['fact'], 'invalid_at': _iso(r['invalid_at'])} for r in unexpired
        ],  # fmt: skip
        'proposed_repairs': repairs,
    }


def report(result: dict[str, Any], exposure: int) -> str:
    lines = [
        f'# Graph audit: {result["facts"]} facts, {result["open_facts"]} open',
        '',
        f'- **Two current values** (review): {len(result["two_current_values"])} subject/relation pairs',
        f'- **Exposed** (>= {exposure} superseded facts in one relation): {len(result["exposed"])}',
        f'- **Inverted intervals:** {len(result["inverted"])}',
        f'- **Zero-length intervals:** {len(result["zero_length"])}',
        f'- **Ended but not expired:** {len(result["ended_not_expired"])}',
        '',
    ]
    for d in result['two_current_values'][:20]:
        facts = '; '.join(f'"{f["fact"]}" (from {(f["valid_at"] or "?")[:10]})' for f in d['open'])
        lines.append(f'- {d["subject"]} / {d["relation"]}: {facts}')
    if result['proposed_repairs']:
        lines += ['', '## Proposed repairs (not applied; review each first)', '']
        for r in result['proposed_repairs'][:20]:
            lines += [f'- End "{r["end"]}" at {r["at"][:10]}, because "{r["because"]}":',
                      f'  `{r["cypher"]}`']  # fmt: skip
    return '\n'.join(lines)


async def main(argv: list[str] | None = None) -> int:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser(prog='graphiti-gate audit')
    ap.add_argument('--group', action='append')
    ap.add_argument('--exposure', type=int, default=9)
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    try:
        result = await audit(driver, args.group, args.exposure)
    finally:
        await driver.close()
    print(json.dumps(result, indent=1, default=str) if args.json else report(result, args.exposure))
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
