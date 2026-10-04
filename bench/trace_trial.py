"""Trace one random-history trial: what the judge was shown at each step, and which home is wrong.

Run with a revision's python: PY bench/trace_trial.py TRIAL CITY (props-1 used --max-homes 30).
"""

import asyncio
import json
import logging
import os
import random
import sys

from graphiti_core.driver.neo4j_driver import Neo4jDriver

import graphiti_gate.judges as J
from graphiti_gate.properties import trial

logging.getLogger('neo4j').setLevel(logging.ERROR)
target = sys.argv[2]  # the city whose interval is wrong
orig = J.Judge._generate_response


async def traced(self, messages, response_model=None, *a, **k):
    out = await orig(self, messages, response_model, *a, **k)
    if getattr(response_model, '__name__', '') == 'EdgeDuplicate':
        c = self.calls[-1]
        alice = [f for f in c.shown if f.startswith('Alice')]
        print(
            f'NEW {c.new_fact!r}: shown {len(c.shown)} ({len(alice)} Alice); target shown={any(target in f for f in c.shown)}; contradicted={c.contradicted}'
        )
    return out


J.Judge._generate_response = traced


async def main():
    d = Neo4jDriver('bolt://localhost:7687', 'neo4j', os.environ['NEO4J_PASSWORD'])
    r = await trial(d, random.Random(int(sys.argv[1])), 30)
    print(json.dumps(r['mismatches']))
    await d.close()


asyncio.run(main())
