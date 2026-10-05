# graphiti-temporal

**Graphiti can fail to retire the current fact once a subject has a long history. This repo
has the fix, the measurements behind it, and a PR gate and fuzzer for Graphiti's memory logic.**

[Graphiti](https://github.com/getzep/graphiti) invalidates a fact when a newer one contradicts it,
so you can ask what is true now. To find what a new fact contradicts, it searches for 10
candidates with no time filter. The subject's superseded history fills those places, the current
fact is never shown to the model, and the graph ends up with two current values.

**Measured** through Graphiti's full `add_episode` on Neo4j, with Codex `gpt-5.6-luna` as the LLM,
a semantic embedder, facts worded six ways and other people in the same group: after 20 past
homes, a move retired the current home

| | Graphiti main | with the fix |
|---|---|---|
| current home retired | 12/30 | **30/30** |
| median tokens per episode | 14,995 | 9,822 |

30 seeds paired between the arms; exact McNemar p = 0.000008. With a lexical embedder the fault
reaches 0/12 at 50 past homes (12/12 with the fix); with a perfect model in place of the LLM it
starts at 10 past values. Scope and caveats: FINDINGS.md, F1.

**Upstream:** issue [#1956](https://github.com/getzep/graphiti/issues/1956), PR
[#1957](https://github.com/getzep/graphiti/pull/1957) (28 lines, CI green, awaiting review). It
can also be applied at runtime:

```python
from graphiti_temporal import fix

fix.apply_invalidation_filter()  # fix 1: before using Graphiti
fix.apply_backfill_neighbors()  # fix 3, optional: facts added out of order (below)
```

**The tool: [graphiti-gate](GATE.md).** It reports what any revision or PR does to Graphiti's
memory, in about a minute, with no model:

- 34 scenarios taken from Graphiti's issues, judges that make real models' mistakes, and rules
  checked on random histories; it exits non-zero when a PR breaks one, so CI goes red.
- On bugs injected into Graphiti and held out from its design, it caught 4 of 6 against 1 of 6 for
  Graphiti's own 500 tests. On 13 real past bugs, the frozen gate caught 0 (they were a different
  class); a pre-registered test on future fixes is in [PROSPECTIVE.md](PROSPECTIVE.md).
- A bisect found the commit behind #1734; a triage of the open PR queue shows which temporal PRs
  fix what.
- A fuzzer runs random and adversarial histories on Neo4j, FalkorDB and Kuzu and shrinks what
  fails. It confirmed #488 on all three, and its crowded mode found that a first version of fix 3
  was wrong on FalkorDB. It has not yet found a bug nobody knew about.

**What is here**

- [FINDINGS.md](FINDINGS.md): each finding with evidence, an intent label (unintended /
  simplification / not a fault), status, and fix. It also lists what turned out *not* to be a
  fault, such as `lucene_sanitize` (#1302).
- [DESIGN.md](DESIGN.md): how each measurement isolates Graphiti's code from the model, why the
  fix is safe, and the mistakes this repository made and corrected. One of them is a first
  version of the fix that an independent review proved wrong.
- `upstream/01-invalidation-candidates.patch`: the fix as a change to graphiti-core, with tests
  in Graphiti's style. It applies cleanly to main at `b7fc30f`. Graphiti's CI unit suite passes
  with it, and the new tests fail when the fix, or its use of duplicate candidates, is reverted.
- `upstream/03-backfill-temporal-neighbors.patch`: fix 3, for the case fix 1 leaves open. An
  older fact added late must end where the subject's next fact begins; at depth that fact is
  crowded out of the candidates. Fix 3 adds the subject's nearest facts in time as candidates.
  Back-filled facts ended in the right place in all 17 random histories of 31-86 facts, where
  showing 30 candidates instead of 10 got 3 right, and fix 1 alone got 3 of 19 right at 20-30
  facts (oracle judge, Neo4j, Graphiti main `b7fc30f`; FINDINGS.md, "Fix 3"). Not yet proposed
  upstream; installable today with `fix.apply_backfill_neighbors()` after
  `fix.apply_invalidation_filter()`.
- `upstream/tests/test_temporal_regressions.py`: regression tests in Graphiti's style for the
  depth fault, the back-fill fault and the OR-ed date filter (#488). `bench/regression_pack.py`
  runs them on main and on each fix: every test except two shallow controls fails on main and
  passes only with its fix, on Neo4j and FalkorDB (`results/regression_pack.jsonl`).
- `bench/`: every measurement, plus `summarize.py`, which regenerates every table from
  `results/`.
- `graphiti_gate/` and [GATE.md](GATE.md): the gate. `ci/graphiti-memory-gate.yml` is a drop-in CI
  workflow for Graphiti.

**Why the fix is safe.** The filter keeps out only facts that ended before the earliest start
among the new fact and its possible duplicates. Graphiti's own interval rules would ignore those
facts anyway. Through Graphiti's real resolver, this repository checked 2,000 random histories and
a separate Codex review checked 30,000 more. Neither found a change to which fact gets resolved, its
dates, or what gets retired. The two exceptions (inverted intervals, and `expired_at` on an
already-ended fact) are documented and tested. The obvious alternative, "search only facts still
valid", changes 284 of 400 decisions, because it breaks back-filling.

## Reproduce

```bash
docker run -d --name gt-neo4j -p 7687:7687 -e NEO4J_AUTH=neo4j/$NEO4J_PASSWORD neo4j:5.26.2
uv sync --extra dev
uv run pytest                            # 2,039 tests, no database or model needed
NEO4J_PASSWORD=... bench/run_oracle.sh   # oracle sweeps and as-of search, about 30 min
NEO4J_PASSWORD=... bench/run_real.sh     # real-model arms; needs the Codex CLI signed in
uv run python bench/summarize.py         # every table, from results/
```

The first measurements used graphiti-core `4f98b7c` (0.30.2); the later ones, including the
realistic run and fix 3, use main at `b7fc30f`. Neo4j is 5.26.2, the version in Graphiti's CI. The
real-model runs use the Codex CLI as Graphiti's LLM (`graphiti_temporal/codex_llm.py`), so no API
key is needed.

## Limits

- The model-based measurements are on Neo4j only. The regression tests also run on FalkorDB,
  and the fuzzer on FalkorDB and Kuzu. Neptune is not tested.
- Most runs use a lexical hash embedder, so they need no model. One run uses a semantic embedder,
  paraphrased facts and other people in the group (FINDINGS.md, "realistic retrieval"): the
  current home was retired 12/30 times on main and 30/30 with the fix, at 20 past homes.
- All real-model runs use one model (gpt-5.6-luna), with 12 or 30 seeds per arm. Fix 3 has not
  yet been run with a real model in its current form.

## License

Apache-2.0
