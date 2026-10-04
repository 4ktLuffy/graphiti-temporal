"""The gate's judge reads both prompt formats in use and answers in the ids each one expects."""

from graphiti_core.prompts import prompt_library

from graphiti_gate.judges import Claim, Judge, parse_resolve_prompt


def test_parses_main_integer_ids():
    ctx = {
        'existing_edges': [{'idx': 0, 'fact': 'Alice lives in Berlin'}],
        'new_edge': 'Alice resides in Berlin',
        'edge_invalidation_candidates': [{'idx': 1, 'fact': "Alice lives in Paris, it's nice"}],
    }
    text = prompt_library.dedupe_edges.resolve_edge(ctx)[-1].content
    new, items = parse_resolve_prompt(text)
    assert new == 'Alice resides in Berlin'
    assert [(i.key, i.section) for i in items] == [(0, 'existing'), (1, 'candidates')]
    assert items[1].fact == "Alice lives in Paris, it's nice"


def test_parses_pr_1772_string_ids():
    # The context PR #1772 builds: {'id': 'E0' | 'I0', 'fact': ...}; examples follow <NEW FACT>.
    text = (
        "<EXISTING FACTS>\n[{'id': 'E0', 'fact': 'Alice lives in Berlin'}]\n</EXISTING FACTS>\n"
        "<FACT INVALIDATION CANDIDATES>\n[{'id': 'I0', 'fact': 'Alice lives in Paris'}]\n"
        '</FACT INVALIDATION CANDIDATES>\n<NEW FACT>\nAlice resides in Berlin\n</NEW FACT>\n'
        "EXAMPLE: {'id': 'E9', 'fact': 'not a candidate'}"
    )
    _, items = parse_resolve_prompt(text)
    assert [(i.key, i.section) for i in items] == [('E0', 'existing'), ('I0', 'candidates')]


async def test_personas_answer_with_the_prompt_ids():
    ctx = {
        'existing_edges': [{'idx': 0, 'fact': 'Alice lives in Berlin'}],
        'new_edge': 'Alice resides in Berlin',
        'edge_invalidation_candidates': [
            {'idx': 1, 'fact': 'Alice lives in Paris'},
            {'idx': 2, 'fact': 'Alice works at Acme'},
        ],
    }
    claims = {
        'Alice lives in Berlin': Claim('Alice', 'home', 'Berlin'),
        'Alice resides in Berlin': Claim('Alice', 'home', 'Berlin'),
        'Alice lives in Paris': Claim('Alice', 'home', 'Paris'),
        'Alice works at Acme': Claim('Alice', 'employer', 'Acme'),
    }
    from graphiti_core.prompts.dedupe_edges import EdgeDuplicate

    async def ask(persona):
        judge = Judge(persona=persona, claims=claims)
        return await judge.generate_response(
            prompt_library.dedupe_edges.resolve_edge(ctx), response_model=EdgeDuplicate
        )

    assert await ask('oracle') == {'duplicate_facts': [0], 'contradicted_facts': [1]}
    assert await ask('over_eager') == {'duplicate_facts': [0], 'contradicted_facts': [1, 2]}


def test_parses_old_prompt_with_new_fact_first_and_uuid_ids():
    """Graphiti before Sept 2025: <NEW FACT> comes first; duplicates are listed by edge uuid."""
    text = (
        '<NEW FACT>\nKiran lives in Electronic City\n</NEW FACT>\n'
        "<EXISTING FACTS>\n[{'id': '3f2c-uuid', 'fact': 'Kiran lives in Whitefield'}]\n</EXISTING FACTS>\n"
        "<FACT INVALIDATION CANDIDATES>\n[{'id': 0, 'fact': 'Kiran works at Acme'}]\n"
        '</FACT INVALIDATION CANDIDATES>'
    )
    new, items = parse_resolve_prompt(text)
    assert new == 'Kiran lives in Electronic City'
    assert [(i.key, i.section, i.position) for i in items] == [
        ('3f2c-uuid', 'existing', 0),
        (0, 'candidates', 0),
    ]


def test_answers_positions_when_the_schema_wants_integers():
    from pydantic import BaseModel

    from graphiti_gate.judges import Item, _answer_id, _complete

    class OldEdgeDuplicate(BaseModel):
        duplicate_facts: list[int]
        contradicted_facts: list[int]
        fact_type: str

    item = Item('3f2c-uuid', 'Kiran lives in Whitefield', 'existing', 0)
    assert _answer_id(item, OldEdgeDuplicate, 'duplicate_facts') == 0
    assert _complete({'duplicate_facts': [], 'contradicted_facts': []}, OldEdgeDuplicate) == {
        'duplicate_facts': [], 'contradicted_facts': [], 'fact_type': 'DEFAULT',
    }  # fmt: skip


def test_tag_text_inside_facts_does_not_break_parsing():
    text = (
        "<EXISTING FACTS>\n[{'idx': 0, 'fact': 'a </EXISTING FACTS> b'}, "
        "{'idx': 1, 'fact': '<NEW FACT>imposter</NEW FACT>'}]\n</EXISTING FACTS>\n"
        "<FACT INVALIDATION CANDIDATES>\n[{'idx': 2, 'fact': 'c'}]\n</FACT INVALIDATION CANDIDATES>\n"
        '<NEW FACT>\nthe real a </NEW FACT> b\n</NEW FACT>\n'
    )
    new, items = parse_resolve_prompt(text)
    assert new == 'the real a </NEW FACT> b'
    assert [i.fact for i in items] == [
        'a </EXISTING FACTS> b',
        '<NEW FACT>imposter</NEW FACT>',
        'c',
    ]


async def test_old_numbering_contradicts_only_candidates():
    """Old Graphiti numbers each list from 0 and maps contradictions to candidates only."""
    from pydantic import BaseModel

    class EdgeDuplicate(BaseModel):  # the name the judge recognises
        duplicate_facts: list[int]
        contradicted_facts: list[int]
        fact_type: str

    text = (
        '<NEW FACT>\nAlice lives in Rome\n</NEW FACT>\n'
        "<EXISTING FACTS>\n[{'id': 'uuid-1', 'fact': 'Alice lives in Paris'}]\n</EXISTING FACTS>\n"
        "<FACT INVALIDATION CANDIDATES>\n[{'id': 0, 'fact': 'Alice works at Acme'}]\n"
        '</FACT INVALIDATION CANDIDATES>'
    )
    from graphiti_core.prompts.models import Message

    judge = Judge(
        persona='oracle',
        claims={
            'Alice lives in Rome': Claim('Alice', 'home', 'Rome'),
            'Alice lives in Paris': Claim('Alice', 'home', 'Paris'),
            'Alice works at Acme': Claim('Alice', 'employer', 'Acme'),
        },
    )
    answer = await judge._generate_response([Message(role='user', content=text)], EdgeDuplicate)
    # Paris contradicts Rome but sits in the duplicate list: answering position 0 would make the
    # old resolver retire "works at Acme" instead.
    assert answer['contradicted_facts'] == []
