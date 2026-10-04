"""The scenario format: a starting graph, incoming facts, a judge, and the graph expected afterwards.

```yaml
id: invalidation/depth-20
title: The current home is retired after 20 past homes
source: graphiti-temporal FINDINGS.md F1
family: history-depth
status: expected          # expected | known_gap | disputed
judge: oracle             # oracle | over_eager | under_resolving | scripted
via: resolve              # resolve (resolve_extracted_edges) | triplet (Graphiti.add_triplet)
facts:                    # the graph before; `claim` is [subject, slot, value] for the judge
  - {id: live, src: Alice, rel: LIVES_IN, dst: Tokyo, fact: Alice has lived in Tokyo since 2019,
     valid: 2019-01-01, claim: [Alice, home, Tokyo]}
moves: {subject: Alice, rel: LIVES_IN, count: 20, start: 1950, slot: home}   # optional generator
incoming:
  - {id: new, src: Alice, rel: LIVES_IN, dst: Berlin, fact: Alice lives in Berlin,
     valid: 2024-06-01, claim: [Alice, home, Berlin]}
expect:
  live: {invalid_at: 2024-06-01}   # or {open: true}; `expired: true|false` is also checked
  new: {open: true}
  open_in_slot: {Alice/home: [new]}   # exactly these facts are open for the slot afterwards
```

Expected outcomes cite where they come from. `known_gap` marks an outcome Graphiti does not reach
today, so the gate reports when a revision reaches it. `disputed` scenarios are run and shown but
never counted as passing or failing.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class FactSpec(BaseModel):
    id: str
    src: str
    rel: str
    dst: str
    fact: str
    valid: date | datetime | None = None
    invalid: date | datetime | None = None
    claim: tuple[str, str, str] | None = None
    episodes: int = 1

    def valid_at(self) -> datetime | None:
        return _utc(self.valid)

    def invalid_at(self) -> datetime | None:
        return _utc(self.invalid)


class Moves(BaseModel):
    """`count` superseded homes for `subject`, each two years long, then nothing open."""

    subject: str
    rel: str
    count: int
    start: int = 1950
    slot: str = 'home'
    template: str = '{subject} lives in {city}'


class Expectation(BaseModel):
    open: bool | None = None
    merged_into: str | None = None  # the incoming fact was resolved as a duplicate of this fact
    episodes: int | None = None  # how many episodes the stored edge cites afterwards
    merged: bool | None = None  # false: the incoming fact must stay its own edge
    invalid_at: date | datetime | None = None
    expired: bool | None = None


class Scenario(BaseModel):
    id: str
    title: str
    source: str
    family: str
    status: Literal['expected', 'known_gap', 'disputed'] = 'expected'
    judge: Literal['oracle', 'over_eager', 'under_resolving', 'scripted', 'noisy'] = 'oracle'
    via: Literal['resolve', 'triplet', 'search', 'nodes', 'bulk_dedupe'] = 'resolve'
    facts: list[FactSpec] = Field(default_factory=list)
    moves: Moves | None = None
    incoming: list[FactSpec] = Field(default_factory=list)
    script: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    expect: dict[str, Expectation] = Field(default_factory=dict)
    open_in_slot: dict[str, list[str]] = Field(default_factory=dict)
    note: str = ''
    # Modes in vias.py
    search: dict[str, Any] = Field(default_factory=dict)
    nodes: list[dict[str, str]] = Field(default_factory=list)
    incoming_nodes: list[str] = Field(default_factory=list)
    expect_nodes: dict[str, str] = Field(default_factory=dict)
    bulk: list[list[dict[str, Any]]] = Field(default_factory=list)
    expect_bulk: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode='before')
    @classmethod
    def _split_expect(cls, data: Any) -> Any:
        expect = dict(data.get('expect') or {})
        if 'open_in_slot' in expect:
            data = {**data, 'open_in_slot': expect.pop('open_in_slot'), 'expect': expect}
        return data

    @model_validator(mode='after')
    def _check_ids(self) -> Scenario:
        ids = [f.id for f in self.facts + self.incoming]
        if len(ids) != len(set(ids)):
            raise ValueError(f'{self.id}: duplicate fact ids')
        unknown = (
            (set(self.expect) | {e.merged_into for e in self.expect.values() if e.merged_into})
            - set(ids)
            - {None}
        )
        if unknown:
            raise ValueError(f'{self.id}: expectations for unknown facts {sorted(unknown)}')
        return self


def _utc(value: date | datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def load(paths: list[Path]) -> list[Scenario]:
    files: list[Path] = []
    for p in paths:
        files += sorted(p.rglob('*.yaml')) if p.is_dir() else [p]
    return [Scenario.model_validate(yaml.safe_load(f.read_text())) for f in files]
