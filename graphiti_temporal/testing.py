"""Deterministic stand-ins for the model calls Graphiti makes, so a measurement isolates retrieval.

`OracleLLM` answers Graphiti's edge-resolution prompt the way a perfect model would, from a ground
truth registered per fact. When an oracle misses a contradiction, the only possible cause is that
the contradicted fact was never shown to it, which is the retrieval behaviour under test.
"""

from __future__ import annotations

import ast
import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from graphiti_core.prompts.models import Message

DIM = 256


def hash_embedding(text: str, dim: int = DIM) -> list[float]:
    """Bag-of-words vector: facts sharing words are similar, with no model and no network."""
    v = [0.0] * dim
    for word in re.findall(r'\w+', text.lower()):
        v[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class HashEmbedder(EmbedderClient):
    async def create(self, input_data: Any) -> list[float]:
        if isinstance(input_data, str):
            return hash_embedding(input_data)
        return hash_embedding(' '.join(str(x) for x in input_data))

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return [hash_embedding(x) for x in input_data_list]


class NullCrossEncoder(CrossEncoderClient):
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        return [(p, 0.0) for p in passages]


@dataclass(frozen=True)
class Claim:
    """What a fact asserts: `subject`'s `slot` has `value`. One value per slot at a time."""

    subject: str
    slot: str
    value: str


def _between(text: str, tag: str) -> list[dict[str, Any]]:
    m = re.search(rf'<{tag}>\s*(.*?)\s*</{tag}>', text, re.DOTALL)
    return ast.literal_eval(m.group(1)) if m else []


@dataclass
class ResolveCall:
    new_fact: str
    duplicate_candidates: list[str]
    invalidation_candidates: list[str]


@dataclass
class OracleLLM(LLMClient):
    """Answers `dedupe_edges.resolve_edge` from ground truth; returns no dates when asked for them."""

    claims: dict[str, Claim] = field(default_factory=dict)
    calls: list[ResolveCall] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__(LLMConfig(), cache=False)

    def register(self, items: Iterable[tuple[str, Claim]]) -> None:
        self.claims.update(items)

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        name = getattr(response_model, '__name__', '')
        if name == 'EdgeTimestamps':
            return {'valid_at': None, 'invalid_at': None}
        if name != 'EdgeDuplicate':
            raise NotImplementedError(f'OracleLLM does not answer {name or "free text"}')

        text = messages[-1].content
        existing = _between(text, 'EXISTING FACTS')
        candidates = _between(text, 'FACT INVALIDATION CANDIDATES')
        new_fact = re.search(r'<NEW FACT>\s*(.*?)\s*</NEW FACT>', text, re.DOTALL).group(1)
        self.calls.append(
            ResolveCall(new_fact, [e['fact'] for e in existing], [c['fact'] for c in candidates])
        )

        new = self.claims[new_fact]
        duplicates, contradicted = [], []
        for item in existing + candidates:
            old = self.claims.get(item['fact'])
            if old is None or (old.subject, old.slot) != (new.subject, new.slot):
                continue
            if old.value == new.value:
                if item in existing:
                    duplicates.append(item['idx'])
            else:
                contradicted.append(item['idx'])
        return {'duplicate_facts': duplicates, 'contradicted_facts': contradicted}
