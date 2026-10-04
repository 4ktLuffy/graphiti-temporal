"""A real model as the judge: Codex answers each revision's own prompts, with a replay cache.

The personas in `judges.py` test Graphiti's code against fixed model behaviour. A PR that changes
a prompt (#1772, #1940) only shows its effect through a real model's answers, as a rate. Here
Codex (via `graphiti_temporal.codex_llm`) answers every prompt the revision sends, and each answer
is recorded by a hash of the exact prompt, schema, model and repetition:

- a rerun replays recorded answers and costs nothing;
- a revision whose prompts differ misses the cache, and the miss list names the prompts it
  changed.

Enabled in the worker by GATE_JUDGE=live (GATE_LIVE_MODEL, GATE_LIVE_EFFORT, GATE_LIVE_CACHE).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from graphiti_core.prompts.models import Message

from graphiti_gate.judges import Call, Judge, parse_resolve_prompt
from graphiti_temporal.codex_llm import CodexLLMClient

CACHE = Path(os.environ.get('GATE_LIVE_CACHE', 'results/gate/live-cache.jsonl'))


def _load() -> dict[str, Any]:
    if not CACHE.exists():
        return {}
    out = {}
    for line in CACHE.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec['key']] = rec['reply']
    return out


class LiveJudge(Judge):
    """Codex answers; the scenario's persona and claims are kept only to label the run."""

    def __init__(self, *args: Any, repetition: int = 0, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.repetition = repetition
        self.model = os.environ.get('GATE_LIVE_MODEL', 'gpt-5.6-luna')
        self.effort = os.environ.get('GATE_LIVE_EFFORT', 'low')
        self.codex = CodexLLMClient(self.model, self.effort)
        self.hits = 0
        self.misses: list[str] = []

    def _key(self, messages: list[Message], response_model: Any) -> str:
        schema = (
            json.dumps(response_model.model_json_schema(), sort_keys=True) if response_model else ''
        )
        # Structured, so different splits of the same text into messages never share a key.
        payload = json.dumps(
            [self.model, self.effort, self.repetition, schema,
             [[m.role, m.content] for m in messages]],
        )  # fmt: skip
        return hashlib.sha256(payload.encode()).hexdigest()

    async def _generate_response(
        self, messages, response_model=None, max_tokens=0, model_size=None
    ):
        key = self._key(messages, response_model)
        cache = _load()
        if key in cache:
            self.hits += 1
            reply = cache[key]
        else:
            self.misses.append(getattr(response_model, '__name__', 'text'))
            reply = await self.codex._generate_response(messages, response_model)
            CACHE.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE, 'a') as f:  # several workers may append at once
                fcntl.flock(f, fcntl.LOCK_EX)
                f.write(json.dumps({'key': key, 'model': self.model, 'reply': reply}) + '\n')
                fcntl.flock(f, fcntl.LOCK_UN)
        if getattr(response_model, '__name__', '') == 'EdgeDuplicate':
            new_fact, items = parse_resolve_prompt(messages[-1].content)
            by_key = {i.key: i.fact for i in items}
            self.calls.append(
                Call(
                    new_fact,
                    [i.fact for i in items],
                    [by_key.get(k, f'?{k}') for k in reply.get('duplicate_facts', [])],
                    [by_key.get(k, f'?{k}') for k in reply.get('contradicted_facts', [])],
                )
            )
        return reply
