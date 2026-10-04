"""Small, plausible bugs injected into Graphiti's resolution logic, to measure what catches them.

Each mutant is one exact text replacement in graphiti-core at the base revision; it must match
exactly once, or it is rejected. The same mutants run against Graphiti's own unit tests and against
the gate, so the two catch rates are comparable. They are the kind of change a refactor or a
plausible-looking PR makes: a flipped comparison, a dropped stamp, an off-by-one.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

EO = 'graphiti_core/utils/maintenance/edge_operations.py'
RECIPES = 'graphiti_core/search/search_config_recipes.py'


@dataclass(frozen=True)
class Mutant:
    id: str
    file: str
    old: str
    new: str
    what: str


MUTANTS = [
    Mutant('skip-ended-boundary', EO,
           'and edge_invalid_at_utc <= resolved_edge_valid_at_utc',
           'and edge_invalid_at_utc < resolved_edge_valid_at_utc',
           'a fact ending exactly when the new one starts is no longer skipped'),
    Mutant('skip-new-ended-boundary', EO,
           'and resolved_edge_invalid_at_utc <= edge_valid_at_utc',
           'and resolved_edge_invalid_at_utc < edge_valid_at_utc',
           'a new fact ending exactly when the old one starts is no longer skipped'),
    Mutant('retire-same-start', EO,
           'and edge_valid_at_utc < resolved_edge_valid_at_utc',
           'and edge_valid_at_utc <= resolved_edge_valid_at_utc',
           'a fact starting the same day as the new one is retired'),
    Mutant('retire-at-new-end', EO,
           'edge.invalid_at = resolved_edge.valid_at',
           'edge.invalid_at = resolved_edge.invalid_at or resolved_edge.valid_at',
           'a retired fact ends at the new fact\'s end instead of its start'),
    Mutant('no-expired-stamp', EO,
           'edge.expired_at = edge.expired_at if edge.expired_at is not None else utc_now()',
           'edge.expired_at = edge.expired_at',
           'retired facts are not stamped expired'),
    Mutant('backfill-latest', EO,
           'invalidation_candidates.sort(key=lambda c: (c.valid_at is None, ensure_utc(c.valid_at)))',
           'invalidation_candidates.sort(key=lambda c: (c.valid_at is None, ensure_utc(c.valid_at)), reverse=True)',
           'a back-filled fact is ended by the latest later fact, not the next one'),
    Mutant('backfill-not-ended', EO,
           'and candidate_valid_at_utc > resolved_edge_valid_at_utc',
           'and candidate_valid_at_utc > resolved_edge_valid_at_utc and False',
           'a back-filled fact is never ended by a later fact'),
    Mutant('backfill-ends-at-own', EO,
           'resolved_edge.invalid_at = candidate.valid_at',
           'resolved_edge.invalid_at = resolved_edge.valid_at',
           'a back-filled fact is ended at its own start'),
    Mutant('no-born-ended-expiry', EO,
           'if resolved_edge.invalid_at and not resolved_edge.expired_at:',
           'if False:',
           'a fact that arrives already ended is not stamped expired'),
    Mutant('index-offset', EO,
           'invalidation_idx_offset = len(related_edges)',
           'invalidation_idx_offset = len(related_edges) + 1',
           'candidate ids are shifted by one'),
    Mutant('no-exact-fast-path', EO,
           'and _normalize_string_exact(edge.fact) == normalized_fact',
           'and _normalize_string_exact(edge.fact) == normalized_fact and False',
           'an exact restatement goes to the model instead of merging'),
    Mutant('last-duplicate', EO,
           'resolved_edge = related_edges[duplicate_fact_id]\n        break',
           'resolved_edge = related_edges[duplicate_fact_id]',
           'the last duplicate wins instead of the first'),
    Mutant('no-episode-provenance', EO,
           'resolved_edge.episodes.append(episode.uuid)',
           'pass',
           'a merged duplicate does not record the new episode'),
    Mutant('duplicates-unscoped', EO,
           'search_filter=SearchFilters(edge_uuids=[edge.uuid for edge in valid_edges]),',
           'search_filter=SearchFilters(),',
           'duplicate candidates are searched across the whole graph, not between the two entities'),
    Mutant('half-candidates', RECIPES,
           '        reranker=EdgeReranker.rrf,\n    )\n)\n',
           '        reranker=EdgeReranker.rrf,\n    ),\n    limit=5,\n)\n',
           'the edge search recipe returns 5 results instead of 10'),
]  # fmt: skip


def patch_for(m: Mutant, root: Path) -> str:
    """A unified diff applying `m` to the source tree at `root`; raises if `old` is not unique."""
    path = root / m.file
    text = path.read_text()
    count = text.count(m.old)
    if count != 1:
        raise ValueError(f'mutant {m.id}: expected one match in {m.file}, found {count}')
    mutated = text.replace(m.old, m.new)
    return ''.join(
        difflib.unified_diff(
            text.splitlines(keepends=True),
            mutated.splitlines(keepends=True),
            fromfile=f'a/{m.file}',
            tofile=f'b/{m.file}',
        )
    )


# Written before any round-1 result was used to add scenarios, and never used to design one: these
# measure whether the gate catches bugs it was not built around.
HELD_OUT = [
    Mutant('held-retired-not-returned', EO,
           '            invalidated_edges.append(edge)\n',
           '            pass\n',
           'a retired fact is changed in memory but not returned, so never saved'),
    Mutant('held-duplicate-off-by-one', EO,
           'duplicate_fact_ids: list[int] = [i for i in duplicate_facts if 0 <= i < len(related_edges)]',
           'duplicate_fact_ids: list[int] = [i for i in duplicate_facts if 0 < i < len(related_edges)]',
           'the first duplicate candidate can never be chosen'),
    Mutant('held-expiry-inverted', EO,
           '    if resolved_edge.expired_at is None:',
           '    if resolved_edge.expired_at is not None:',
           'the back-fill expiry runs only for facts already expired'),
    Mutant('held-batch-dedup-loose', EO,
           '            edge.source_node_uuid,\n            edge.target_node_uuid,\n            _normalize_string_exact(edge.fact),',
           '            edge.source_node_uuid,\n            _normalize_string_exact(edge.fact),',
           'two extracted facts with the same text but different targets collapse into one'),
    Mutant('held-candidates-by-name', EO,
           '                extracted_edge.fact,\n                group_ids=[extracted_edge.group_id],\n                config=EDGE_HYBRID_SEARCH_RRF,\n                search_filter=SearchFilters(),',
           '                extracted_edge.name,\n                group_ids=[extracted_edge.group_id],\n                config=EDGE_HYBRID_SEARCH_RRF,\n                search_filter=SearchFilters(),',
           'invalidation candidates are searched by relation name instead of the fact'),
    Mutant('held-keep-overlap', EO,
           'edge for edge in invalidation_result.edges if edge.uuid not in related_uuids',
           'edge for edge in invalidation_result.edges',
           'a fact can appear as both duplicate and invalidation candidate'),
]  # fmt: skip
