# Reproductions for open Graphiti issues

One-off scripts, kept exactly as they were run to reach each verdict. Run each with Python and a
Graphiti install (current main), against local databases:

- Neo4j: `NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=...`
- FalkorDB: on localhost; pass the port as an argument where the script takes one (6.x and 4.x
  were compared on 6379 and 6380).

No model or API key is needed: the scripts use stub embedders and LLMs.

| issue | script | verdict |
|---|---|---|
| #1947 FalkorDB 6.x | `1947/repro.py PORT` | reproduced: node fulltext index creation fails on 6.0.1, works on 4.10.3 |
| #1885 nested attributes | `1885/repro.py`, `1885/roundtrip.py` | reproduced on Neo4j and FalkorDB 6/4; with PR #1886 nested values read back as JSON strings |
| #1505 NaN embeddings | `1505/repro.py` | partial: Neo4j fails loudly; the silent miss is limited to Python-side comparisons |
| #1627 entities without relationships | `1627/repro.py` | intended: `search()` is edge-only; `search_()` returns the entity and episode |
| #1842 FalkorDB multi-group reads | `1842/repro.py` | `Graphiti.*` reads are correct on main; direct `*.get_by_group_ids` calls still read one graph |
