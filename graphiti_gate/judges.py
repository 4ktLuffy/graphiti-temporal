"""Stand-ins for the model in Graphiti's edge-resolution step, with the errors real models make.

A scenario names a judge persona:

- `oracle`: rules from ground truth. A fact asserting the same value for the same subject and slot
  is a duplicate; a different value for that slot is a contradiction.
- `over_eager`: also calls every other fact about the same subject a contradiction. This is the
  error behind #1728, where unrelated facts retire each other.
- `under_resolving`: the oracle, but it returns nothing for about half the questions, chosen
  deterministically per fact. #1772 measured this on deepseek-v4-flash.
- `scripted`: the scenario lists, per incoming fact, the duplicates and contradictions to report.

The judge reads the facts out of whatever prompt the revision sends and answers in whatever ids that
revision uses: integer `idx` on main, `"E0"`/`"I2"` strings in #1772. A gate that only spoke one
format could not judge the PRs that change it.
"""

from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from graphiti_core.prompts.models import Message

PERSONAS = ('oracle', 'over_eager', 'under_resolving', 'scripted')
_ITEM = re.compile(r"\{'(?:idx|id)':[^{}]*?'fact':\s*(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")\}")


class JudgeError(RuntimeError):
    """The revision asked something no persona can answer, e.g. an extraction prompt."""


@dataclass(frozen=True)
class Claim:
    subject: str
    slot: str
    value: str


@dataclass
class Item:
    key: Any  # the id exactly as the prompt gave it: 3, 'E3', or (in old versions) an edge uuid
    fact: str
    section: str  # 'existing' (duplicate candidates) or 'candidates' (invalidation candidates)
    position: int  # index within its own list


def _list_span(text: str, tag: str) -> tuple[int, int, list] | None:
    """(start, end, parsed list) of `<tag>[...]</tag>`. A closing tag inside a fact's text cannot
    end the list early: each closing tag is tried until the content parses as a list literal."""
    start = text.find(f'<{tag}>')
    if start < 0:
        return None
    body = start + len(tag) + 2
    close = text.find(f'</{tag}>', body)
    while close >= 0:
        try:
            value = ast.literal_eval(text[body:close].strip())
            if isinstance(value, list):
                return start, close + len(tag) + 3, value
        except (ValueError, SyntaxError):
            pass
        close = text.find(f'</{tag}>', close + 1)
    return None


def parse_resolve_prompt(text: str) -> tuple[str, list[Item]]:
    """The new fact and every listed fact, from any revision's `dedupe_edges.resolve_edge` prompt.

    Lists are read as Python literals between their own tags, so section order, worked examples
    elsewhere, and tag text inside facts do not matter. The new fact is found outside the lists.
    """
    items, spans = [], []
    for tag, section in (
        ('EXISTING FACTS', 'existing'),
        ('FACT INVALIDATION CANDIDATES', 'candidates'),
    ):
        found = _list_span(text, tag)
        if found is None:
            continue
        start, end, value = found
        spans.append((start, end))
        for position, d in enumerate(v for v in value if isinstance(v, dict) and 'fact' in v):
            items.append(Item(d.get('idx', d.get('id')), d['fact'], section, position))

    def outside(i: int) -> bool:
        return not any(a <= i < b for a, b in spans)

    opens = [m.start() for m in re.finditer('<NEW FACT>', text) if outside(m.start())]
    if not opens:
        raise JudgeError('no <NEW FACT> in the edge-resolution prompt')
    start = opens[0] + len('<NEW FACT>')
    later_lists = [a for a, _ in spans if a > start]
    limit = min(later_lists) if later_lists else len(text)
    closes = [m.start() for m in re.finditer('</NEW FACT>', text)
              if start <= m.start() < limit and outside(m.start())]  # fmt: skip
    if not closes:
        raise JudgeError('no </NEW FACT> in the edge-resolution prompt')
    return text[start : closes[-1]].strip(), items


@dataclass
class Call:
    new_fact: str
    shown: list[str]
    duplicates: list[str]
    contradicted: list[str]


@dataclass
class Judge(LLMClient):
    persona: str = 'oracle'
    claims: dict[str, Claim] = field(default_factory=dict)
    script: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    dates: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.persona not in PERSONAS:
            raise ValueError(f'unknown judge persona {self.persona!r}')
        super().__init__(LLMConfig(), cache=False)

    def _rule(self, new_fact: str, item: Item) -> str | None:
        if self.persona == 'scripted':
            rules = self.script.get(new_fact, {})
            if item.fact in rules.get('duplicates', []):
                return 'duplicate'
            return 'contradicted' if item.fact in rules.get('contradicted', []) else None
        new, old = self.claims.get(new_fact), self.claims.get(item.fact)
        if new is None or old is None or old.subject != new.subject:
            return None
        if old.slot == new.slot:
            return 'duplicate' if old.value == new.value else 'contradicted'
        return 'contradicted' if self.persona == 'over_eager' else None

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: Any = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        name = getattr(response_model, '__name__', '')
        if name == 'EdgeTimestamps':
            fact = re.search(r'<FACT>\s*(.*?)\s*</FACT>', messages[-1].content, re.S)
            valid, invalid = self.dates.get(fact.group(1).strip() if fact else '', (None, None))
            return {'valid_at': valid, 'invalid_at': invalid}
        if name != 'EdgeDuplicate':
            raise JudgeError(f'the gate does not answer {name or "free-text"} prompts')

        new_fact, items = parse_resolve_prompt(messages[-1].content)
        duplicates, contradicted = [], []
        silent = self.persona == 'under_resolving' and (
            int(hashlib.sha256(new_fact.encode()).hexdigest(), 16) % 2 == 0
        )
        # Old versions number each list from 0 and read contradictions against the candidate list
        # only; offering a duplicate-list fact there would retire the wrong candidate.
        per_section = any(not isinstance(i.key, int) for i in items if i.section == 'existing') or (
            any(i.section == 'existing' for i in items)
            and any(i.section == 'candidates' and i.key == 0 for i in items)
        )
        for item in items:
            rule = None if silent else self._rule(new_fact, item)
            if per_section and rule == 'contradicted' and item.section != 'candidates':
                rule = None
            if rule == 'duplicate' and item.section == 'existing':
                duplicates.append(item)
            elif rule == 'contradicted':
                contradicted.append(item)
        self.calls.append(
            Call(
                new_fact,
                [i.fact for i in items],
                [i.fact for i in duplicates],
                [i.fact for i in contradicted],
            )
        )
        answer = {
            'duplicate_facts': [_answer_id(i, response_model, 'duplicate_facts') for i in duplicates],
            'contradicted_facts': [_answer_id(i, response_model, 'contradicted_facts') for i in contradicted],
        }  # fmt: skip
        return _complete(answer, response_model)


def _complete(answer: dict[str, Any], response_model: Any) -> dict[str, Any]:
    """Fill fields other revisions require (e.g. `fact_type` in older Graphiti) with neutral values."""
    neutral = {str: 'DEFAULT', int: -1, bool: False, list: []}
    for name, f in getattr(response_model, 'model_fields', {}).items():
        if name in answer or not f.is_required():
            continue
        origin = getattr(f.annotation, '__origin__', f.annotation)
        answer[name] = neutral.get(origin)
    return answer


def _answer_id(item: Item, response_model: Any, field: str) -> Any:
    """The id as the revision's schema wants it. Old versions list uuids but expect positions."""
    f = getattr(response_model, 'model_fields', {}).get(field)
    wants_int = f is not None and getattr(f.annotation, '__args__', (None,))[0] is int
    if wants_int and not isinstance(item.key, int):
        return item.position
    return item.key
