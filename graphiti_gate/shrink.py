"""Shrink a failing random history to the smallest one that still breaks the same rule.

A rule failure on a 25-home history is true but hard to read. This removes one home (or one of
the other person's facts) at a time and keeps the removal only if the rule still fails on two
consecutive runs, so Neo4j's tie-breaking cannot fake a reduction. It stops when no single
removal keeps the failure.

    python -m graphiti_gate.shrink --trial 7 --max-homes 30 --rule chronology   # in a revision venv
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random

from graphiti_core.driver.neo4j_driver import Neo4jDriver

from graphiti_gate.properties import history, run_history


def _without_home(homes, order, again, i):
    homes2 = homes[:i] + homes[i + 1 :]
    order2 = [j - (j > i) for j in order if j != i]
    again2 = 0 if again == i else again - (again > i)
    return homes2, order2, again2


async def fails(driver, rule, homes, order, others, again, runs: int = 2) -> dict | None:
    """The last failing result if the rule fails on every one of `runs` runs, else None."""
    result = None
    for _ in range(runs):
        result = await run_history(driver, homes, order, others, again)
        if result[rule]:
            return None
    return result


async def shrink(driver, rule: str, homes, order, others, again) -> dict:
    result = await fails(driver, rule, homes, order, others, again)
    if result is None:
        return {'reproduced': False}
    changed = True
    while changed:
        changed = False
        for i in range(len(homes)):
            if len(homes) <= 2:
                break
            h, o, a = _without_home(homes, order, again, i)
            r = await fails(driver, rule, h, o, others, a)
            if r is not None:
                homes, order, again, result, changed = h, o, a, r, True
                break
        for c in list(others):
            rest = [x for x in others if x != c]
            r = await fails(driver, rule, homes, order, rest, again)
            if r is not None:
                others, result, changed = rest, r, True
    return {
        'reproduced': True,
        'rule': rule,
        'homes': len(homes),
        'other_person_facts': len(others),
        'arrivals': [f'{homes[i][2].year}: {homes[i][0]}' for i in order],
        'mismatches': result.get('mismatches'),
        'open': result.get('open'),
    }


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    logging.getLogger('graphiti_core').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('--trial', type=int, required=True)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--max-homes', type=int, default=25)
    ap.add_argument('--rule', default='chronology')
    args = ap.parse_args()
    driver = Neo4jDriver(
        os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
        os.environ.get('NEO4J_USER', 'neo4j'),
        os.environ['NEO4J_PASSWORD'],
    )
    rng = random.Random(args.seed * 100_000 + args.trial)
    print(json.dumps(await shrink(driver, args.rule, *history(rng, args.max_homes)), indent=1))
    await driver.close()


if __name__ == '__main__':
    asyncio.run(main())
