# graphiti-temporal

**Graphiti forgets to retire old facts once a fact has changed ten times. This finds it, fixes it,
and measures it.**

[Graphiti](https://github.com/getzep/graphiti) invalidates a fact when a newer one contradicts it,
so you can "query what's true now". To find what a new fact contradicts, it searches for 10
candidates with no filter. A subject's superseded history fills those places, so the live fact is
never shown to the model and never retired. The graph then holds two current values.

On Neo4j, with Graphiti's full `add_episode` and a real model (Codex `gpt-5.6-luna`), we measured
how often the current home was retired when someone moved:

| past homes | Graphiti 0.30.2 | with the fix |
|---|---|---|
| 20 | 6/12 | 12/12 |
| 50 | **0/12** | **12/12** |

The runs are paired by seed; exact McNemar p = 0.031 and 0.0005. With a perfect model in place of
the LLM, the fault starts at 10 past values (26/30 retired) and reaches 6/30 at 50. The fix gives
30/30 at every depth.

```python
from graphiti_temporal import fix

fix.apply_invalidation_filter()  # before using Graphiti
```

**And a tool built on it: [graphiti-gate](GATE.md).** It reports what any revision or PR does to
Graphiti's memory, in about a minute, with no model. It works from 29 scenarios taken from
Graphiti's issues, judges that make real models' mistakes, and rules checked on random histories.
On bugs injected into Graphiti that were held out from its design, it caught 4 of 6 against 1 of 6
for Graphiti's own 500 tests. Run on the open PR queue, it shows which of four competing temporal
PRs fixes what, that none breaks the others, and which PRs it cannot judge yet.

**What is here**

- [FINDINGS.md](FINDINGS.md): each finding with evidence, an intent label (unintended /
  simplification / not a fault), status, and fix. It also lists what turned out *not* to be a
  fault, such as `lucene_sanitize` (#1302).
- [DESIGN.md](DESIGN.md): how each measurement isolates Graphiti's code from the model, why the
  fix is safe, and the mistakes this repository made and corrected. One of them is a first
  version of the fix that an independent review proved wrong.
- `upstream/01-invalidation-candidates.patch`: the fix as a change to graphiti-core, with tests
  in Graphiti's style. It applies to main at `5d47d4d`. Graphiti's CI unit suite passes with it,
  and the new tests fail when the fix, or its use of duplicate candidates, is reverted.
- `bench/`: every measurement, plus `summarize.py`, which regenerates every table from
  `results/`.
- `graphiti_gate/` and [GATE.md](GATE.md): the gate. `ci/graphiti-memory-gate.yml` is a drop-in CI
  workflow for Graphiti.

**Why the fix is safe.** The filter keeps out only facts that ended before the earliest start
among the new fact and its possible duplicates. Graphiti's own interval rules would ignore those
facts anyway. Through Graphiti's real resolver, this repository checked 2,000 random histories and
an independent review checked 30,000 more. Neither found a change to which fact gets resolved, its
dates, or what gets retired. The two exceptions (inverted intervals, and `expired_at` on an
already-ended fact) are documented and tested. The obvious alternative, "search only facts still
valid", changes 284 of 400 decisions, because it breaks back-filling.

## Reproduce

```bash
docker run -d --name gt-neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/$NEO4J_PASSWORD neo4j:5.26.2
uv sync --extra dev
uv run pytest                            # 2010 tests, no database or model needed
NEO4J_PASSWORD=... bench/run_oracle.sh   # oracle sweeps and as-of search, about 30 min
NEO4J_PASSWORD=... bench/run_real.sh     # real-model arms; needs the Codex CLI signed in
uv run python bench/summarize.py         # every table, from results/
```

Pinned to graphiti-core `4f98b7c` (0.30.2) and Neo4j 5.26.2, the version in Graphiti's CI. The
real-model runs use the Codex CLI as Graphiti's LLM (`graphiti_temporal/codex_llm.py`), so no API
key is needed.

## Limits

- Measured on Neo4j only. FalkorDB, Kuzu and Neptune share the invalidation code, but their
  search paths differ.
- Embeddings are a lexical hash, so runs need no model. A semantic embedder changes which facts
  tie, not the 10-place cap.
- Real-model arms are 12 seeds each, with one model.

## License

Apache-2.0, the same as Graphiti.
