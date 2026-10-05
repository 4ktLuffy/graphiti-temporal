"""The history-depth fault under realistic retrieval: semantic embeddings, paraphrased facts,
several people, unrelated open facts, and two real models as the judge.

bench/real_llm.py used a lexical hash embedder and one fact template. Here the stored history is
embedded by a local semantic model (BAAI/bge-small-en-v1.5), each fact is worded one of six ways,
three other people have their own paraphrased homes, and Alice also has open facts that must not
be touched (a job, a hobby). One real episode, "Alice moved to Berlin.", then runs through
Graphiti's full `add_episode` with Codex as the LLM, on main and with fix 1, on the same seeds.

Recorded per trial:
- retired: the current home was ended (the outcome);
- shown: the current home was among the candidates shown to the model (candidate recall);
- collateral: other open facts that were ended (should be none);
- tokens and seconds for the episode.

    python bench/realistic.py --n-history 20 --trials 30 --model gpt-5.6-luna --fix 1 --out ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.nodes import EntityNode, EpisodeType
from graphiti_core.utils.datetime_utils import utc_now

from graphiti_temporal.codex_llm import CodexLLMClient, CodexQuotaExhausted
from graphiti_temporal.stats import wilson
from graphiti_temporal.testing import NullCrossEncoder
from graphiti_temporal.world import CITIES, PEOPLE

UTC = timezone.utc
EMBEDDER_MODEL = 'BAAI/bge-small-en-v1.5'
HOME = [
    '{p} lives in {c}',
    '{p} resides in {c}',
    "{p}'s home is in {c}",
    '{p} has made a home in {c}',
    '{p} is based in {c}',
    '{p} rents a flat in {c}',
]
# Open facts about Alice that a move must not end.
UNRELATED = [
    ('WORKS_AT', 'Alice works at Globex', 'Globex'),
    ('ENJOYS', 'Alice enjoys climbing', 'climbing'),
]


class SemanticEmbedder(EmbedderClient):
    def __init__(self):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(EMBEDDER_MODEL)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(texts, normalize_embeddings=True).tolist()

    async def create(self, input_data):
        text = input_data if isinstance(input_data, str) else ' '.join(str(x) for x in input_data)
        return self.embed([text])[0]

    async def create_batch(self, input_data_list):
        return self.embed(input_data_list)


async def build(driver, emb: SemanticEmbedder, n_history: int, seed: int) -> dict:
    rng = random.Random(seed)
    group = f'real-{n_history}-{seed}-{uuid.uuid4().hex[:10]}'
    now, nodes = utc_now(), {}

    async def node(name: str) -> EntityNode:
        if name not in nodes:
            nodes[name] = EntityNode(name=name, group_id=group, labels=['Entity'],
                                     name_embedding=emb.embed([name])[0])  # fmt: skip
            await nodes[name].save(driver)
        return nodes[name]

    plan = []  # (person, relation, fact, target, start, end)
    for person in PEOPLE[:4]:
        k = n_history if person == 'Alice' else 5
        cities = rng.sample(CITIES, k + 1)
        for i, city in enumerate(cities):
            start = datetime(1950 + 2 * i, 1, 1, tzinfo=UTC)
            end = datetime(1952 + 2 * i, 1, 1, tzinfo=UTC) if i < k else None
            plan.append(
                (person, 'LIVES_IN', rng.choice(HOME).format(p=person, c=city), city, start, end)
            )
    for rel, text, target in UNRELATED:
        plan.append(('Alice', rel, text, target, datetime(2015, 1, 1, tzinfo=UTC), None))
    rng.shuffle(plan)

    embeddings = emb.embed([p[2] for p in plan])
    live, watch = None, []
    for (person, rel, text, target, start, end), vec in zip(plan, embeddings, strict=True):
        src, dst = await node(person), await node(target)
        edge = EntityEdge(source_node_uuid=src.uuid, target_node_uuid=dst.uuid, group_id=group,
                          name=rel, fact=text, fact_embedding=vec, episodes=[], created_at=now,
                          valid_at=start, invalid_at=end, expired_at=now if end else None)  # fmt: skip
        await edge.save(driver)
        if end is None:
            if person == 'Alice' and rel == 'LIVES_IN':
                live = edge
            else:
                watch.append(edge)  # open facts a move by Alice must leave open
    assert live is not None and live.valid_at is not None
    return {'group': group, 'live': live, 'watch': watch,
            'moved': datetime(live.valid_at.year + 1, 6, 1, tzinfo=UTC)}  # fmt: skip


# `two-moves` asks whether a change inside one message is caught: both new homes arrive in one
# episode, so they are resolved in one batch and cannot see each other (FINDINGS.md, "One
# episode, two old homes"); only the extraction model can end Bergen.
EPISODES = {
    'move': 'Alice moved to Berlin.',
    'two-moves': 'In {y1} Alice moved to Bergen, and in {y2} she moved again, to Beirut.',
}


async def open_homes(driver, group: str) -> list[str]:
    """Alice's open facts other than the job and hobby that every world starts with."""
    records, _, _ = await driver.execute_query(
        'MATCH (a:Entity {name: "Alice", group_id: $g})-[e:RELATES_TO]->(m:Entity) '
        'WHERE e.invalid_at IS NULL AND e.expired_at IS NULL AND NOT m.name IN $skip '
        'RETURN m.name AS place',
        g=group,
        skip=[target for _, _, target in UNRELATED],
    )
    return sorted(r['place'] for r in records)


async def trial(driver, emb, make_llm, n_history: int, seed: int, episode: str = 'move') -> dict:
    world = await build(driver, emb, n_history, seed)
    y1 = world['moved'].year
    body = EPISODES[episode].format(y1=y1, y2=y1 + 1)
    when = world['moved'] if episode == 'move' else world['moved'].replace(year=y1 + 1)
    llm = make_llm()
    graphiti = Graphiti(graph_driver=driver, llm_client=llm, embedder=emb,
                        cross_encoder=NullCrossEncoder())  # fmt: skip
    error, t0 = None, time.time()
    try:
        await graphiti.add_episode(name=episode, episode_body=body,
                                   source_description='chat', reference_time=when,
                                   source=EpisodeType.text, group_id=world['group'])  # fmt: skip
    except CodexQuotaExhausted:
        raise
    except Exception as e:  # recorded, never dropped
        error = f'{type(e).__name__}: {e}'[:300]
    seconds = round(time.time() - t0, 1)
    live = await EntityEdge.get_by_uuid(driver, world['live'].uuid)
    after = {e.uuid: await EntityEdge.get_by_uuid(driver, e.uuid) for e in world['watch']}
    collateral = [world_e.fact for world_e in world['watch']
                  if after[world_e.uuid].invalid_at is not None or after[world_e.uuid].expired_at is not None]  # fmt: skip
    still_open = await open_homes(driver, world['group'])
    await driver.execute_query('MATCH (n {group_id: $g}) DETACH DELETE n', g=world['group'])
    resolve = [c for c in llm.calls if c['response_model'] == 'EdgeDuplicate']
    return {
        'n_history': n_history,
        'seed': seed,
        'episode': episode,
        'open_homes': still_open,
        'retired': live.invalid_at is not None,
        'shown': any(world['live'].fact in c['prompt'] for c in resolve),
        'collateral': collateral,
        'error': error,
        'seconds': seconds,
        'llm_calls': len(llm.calls),
        'tokens': sum(c.get('tokens') or 0 for c in llm.calls),
    }


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float('nan')


async def main() -> None:
    logging.getLogger('neo4j').setLevel(logging.ERROR)
    ap = argparse.ArgumentParser()
    ap.add_argument('--n-history', type=int, default=20)
    ap.add_argument('--trials', type=int, default=30)
    ap.add_argument('--first-seed', type=int, default=5000)
    ap.add_argument('--parallel', type=int, default=3)
    ap.add_argument('--model', default='gpt-5.6-luna')
    ap.add_argument('--effort', default='low')
    ap.add_argument('--fix', choices=['none', '1', '1+3'], default='none')
    ap.add_argument('--episode', choices=sorted(EPISODES), default='move')
    ap.add_argument('--out', required=True)
    args = ap.parse_args()
    from graphiti_temporal import fix

    if args.fix in ('1', '1+3'):
        fix.apply_invalidation_filter()
    if args.fix == '1+3':
        fix.apply_backfill_neighbors()
    driver = Neo4jDriver(os.environ.get('NEO4J_URI', 'bolt://localhost:7687'),
                         os.environ.get('NEO4J_USER', 'neo4j'), os.environ['NEO4J_PASSWORD'])  # fmt: skip
    await driver.build_indices_and_constraints()
    emb = SemanticEmbedder()
    codex_slots = asyncio.Semaphore(args.parallel * 2)

    def make_llm() -> CodexLLMClient:
        return CodexLLMClient(args.model, args.effort, slots=codex_slots)

    slots, t0 = asyncio.Semaphore(args.parallel), time.time()

    async def one(seed: int) -> dict:
        async with slots:
            r = await trial(driver, emb, make_llm, args.n_history, seed, args.episode)
            print(json.dumps(r), flush=True)
            return r

    try:
        rows = await asyncio.gather(
            *(one(s) for s in range(args.first_seed, args.first_seed + args.trials))
        )
    except CodexQuotaExhausted as e:
        print(f'\n*** CODEX CREDITS EXHAUSTED - reset the account and rerun. {e}', flush=True)
        await driver.close()
        sys.exit(3)
    await driver.close()
    ok = [r for r in rows if r['error'] is None]
    k = sum(r['retired'] for r in ok)
    lo, hi = wilson(k, len(ok))
    secs = [r['seconds'] for r in ok]
    print(f'{args.model}/{args.effort} fix={args.fix} history={args.n_history}: retired {k}/{len(ok)} '
          f'[{lo:.2f}, {hi:.2f}], shown {sum(r["shown"] for r in ok)}/{len(ok)}, '
          f'collateral {sum(bool(r["collateral"]) for r in ok)}, errors {len(rows) - len(ok)}, '
          f'p50 {pct(secs, 0.5)}s p95 {pct(secs, 0.95)}s, {time.time() - t0:.0f}s')  # fmt: skip
    meta = {'fix': args.fix, 'model': args.model, 'effort': args.effort, 'embedder': EMBEDDER_MODEL,
            'neo4j': '5.26.2', 'argv': sys.argv[1:], 'seconds': round(time.time() - t0, 1)}  # fmt: skip
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, 'w') as f:
        f.write(json.dumps({'meta': meta}) + '\n')
        f.writelines(json.dumps(r) + '\n' for r in rows)


if __name__ == '__main__':
    asyncio.run(main())
