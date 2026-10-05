# Findings: Graphiti's temporal memory as a graph accumulates history

**Scope.** getzep/graphiti, graphiti-core at `4f98b7c` (0.30.2). Patch 01 also applies cleanly on
`5d47d4d`, main on 2026-10-04. Database: Neo4j 5.26.2, the version in Graphiti's CI.

**How each finding is judged.** Every finding has:

- evidence you can rerun,
- an intent label,
- a status.

Only findings labelled *unintended* are faults.

**Intent labels.**

- **unintended**: contradicts Graphiti's own docs, code comments, or the behaviour its code
  clearly aims for.
- **simplification**: a deliberate shortcut with a cost.
- **not a fault**: behaves as intended.

**How every number was produced.**

- Tables come from `bench/summarize.py` over `results/*.jsonl`.
- Rates carry 95% Wilson intervals.
- Real-model comparisons are paired by seed (exact McNemar).
- "Oracle" means Graphiti's LLM is replaced by ground truth. A miss can then only come from what
  Graphiti's search showed the model (DESIGN.md).

---

## F1. Once a subject has 10 or more past values of a fact, Graphiti often fails to retire the current one

**Intent: unintended.**

- Graphiti's README says "When information changes, old facts are invalidated" and "Query what's
  true now" (`README.md:118-119`).
- The maintainers' recipe for "what's true now" is an `invalid_at IS NULL` filter (#769).

After this fault, that recipe returns two current values.

**Mechanism.**

- **How the candidates are found.** When a new fact arrives, `resolve_extracted_edges` finds
  facts it might contradict with a hybrid search capped at 10 results
  (`edge_operations.py:407-417`). That search passes `SearchFilters()`, which is no filter, so
  the cap applies to every fact, superseded or not. `Graphiti.add_triplet` does the same
  (`graphiti.py:1789-1797`).
- **What fills the cap.** A subject's superseded history competes with its live fact for the 10
  places. When the live fact is not among them, the model never sees it, so it is never retired.
  The subject then has two "current" values.
- **What the PRs touch.** The four open invalidation PRs change what happens *after* this
  search. #1729 guards which candidates may be retired, #1772 changes the judge's ids, #1940 adds
  same-endpoint facts, and #1867 handles disjoint intervals. None of them changes what the search
  returns, so none fixes this.

**Origin: older than the code that looks responsible.**

- **What #906 changed.** The unfiltered call arrived in #906 ("OpenSearch updates", merged
  2025-09-14; `git log -S` on the line). Before it, candidates came from
  `get_edge_invalidation_candidates`, a query scoped to edges touching the new fact's own entities
  (`search_utils.py:1587`, still in the code and now used only by tests). #906 replaced it with
  the graph-wide hybrid search.
- **What the gate showed.** The gate, run on the commit before #906 (`4dab259`), on #906
  (`3efe085`) and on main, gives the same answer at all three. `depth-20` fails and the 5-home
  control passes. The old query also capped results at 10 and had no time filter, and a person's
  past homes are all their own facts, so scoping never helped.
- **The same holds for #1728.** Its reporter traces unrelated facts retiring each other to #906,
  but the `unrelated-over-eager` scenario also fails before #906, because the job and the new
  fact share the person's node. #906 widened the search; it did not create either fault.
- **How far back.** The fault is present at least since `4dab259` (September 2025) and in every
  version tested since. Versions before Graphiti's driver abstraction (March 2025 and earlier,
  e.g. `dbe21a1`) cannot run the gate without a rewrite, so they were not tested.

**Evidence: oracle, Neo4j, 30 seeds per row.** The table shows how often the current home was
retired when the person moved.

| past homes | no fix | fix 1 |
|---|---|---|
| 0, 5, 9 | 30/30 | 30/30 |
| 10 | 26/30 [0.70, 0.95] | 30/30 [0.89, 1.00] |
| 12 | 20/30 [0.49, 0.81] | 30/30 |
| 20 | 14/30 [0.30, 0.64] | 30/30 |
| 50 | 6/30 [0.10, 0.37] | 30/30 |

In every row, "retired" equals "shown to the model", so the cause is the search. With the fix,
the candidate pool holds 0 superseded facts on average; without it, about 9 of 10.

**Evidence: real model.** Codex `gpt-5.6-luna` (low effort) is Graphiti's LLM for the full
`add_episode` pipeline, on the same seeds per row.

| past homes | no fix | fix 1 | p (exact McNemar) |
|---|---|---|---|
| 20 | 6/12 [0.25, 0.75] | 12/12 [0.76, 1.00] | 0.031 |
| 50 | **0/12** [0.00, 0.24] | **12/12** [0.76, 1.00] | 0.0005 |

With the fix, the live fact was in the model's prompt in all 24 trials, and the person ended with
exactly one current home in all 24. Without it, every failed trial ended with two.

**Evidence: realistic retrieval** (`bench/realistic.py`, `results/realistic/`). The runs above use
a lexical hash embedder and one wording per fact, so they could overstate how often the live fact
is crowded out. Here everything that decides retrieval is realistic:

- a semantic embedder (`BAAI/bge-small-en-v1.5`, run locally);
- each fact worded one of six ways ("resides in", "is based in", "rents a flat in", ...);
- three other people with their own histories in the same group;
- two open facts about Alice that a move must not touch (a job, a hobby).

Alice has 20 past homes. "Alice moved to Berlin." then runs through the full `add_episode` with
Codex `gpt-5.6-luna` (low effort), on 30 seeds paired between main and fix 1.

| | main | fix 1 |
|---|---|---|
| current home retired | 12/30 [0.25, 0.58] | **30/30** [0.89, 1.00] |
| current home shown to the model | 13/30 | 30/30 |
| other open facts wrongly ended | 0 | 0 |
| tokens per episode, median | 14,995 | 9,822 |

- **Paired:** 18 seeds where only the fix retired the home, 0 the other way; exact McNemar
  p = 0.000008.
- **Retrieval explains the outcome.** No home was retired without being shown. One was shown and
  not retired; that miss is the model's.
- **Token cost fell, not rose.** Median tokens per episode went from 14,995 to 9,822 (-34%; the
  mean fell 23%), because stale homes no longer fill the prompt. Median time also fell (112 s to
  41 s), but the arms ran one after the other, and Codex's latency varies, so that is not claimed.
- **Scope.** One model (`gpt-5.6-luna`, low effort), one depth (20 past homes), one embedder,
  Neo4j, and one kind of history: Alice's homes, all one relation, then "Alice moved to Berlin."
  It shows the mechanism survives realistic retrieval; it does not show how often real
  conversation graphs reach this state.
- **Not yet run:** a second model as the judge (gpt-6-astra), stopped when the Codex credits ran
  out; and other depths.

**Fix.** Search candidates only among facts the new fact can affect. These are facts still open,
or ending after the earliest `valid_at` among the new fact and its duplicate candidates. There is
no filter when the new fact has no `valid_at`.

- **The upstream patch.** `upstream/01-invalidation-candidates.patch` is about 35 changed lines in
  `edge_operations.py` and `graphiti.py`, plus 135 lines of tests in Graphiti's own style. Their unit suite and
  ruff pass, and the new tests fail when the fix is reverted.
- **The runtime version.** `graphiti_temporal.fix.apply_invalidation_filter()` installs the same
  change today.

**The fix leaves every decision Graphiti makes unchanged.** The resolved edge, its dates and the
retired facts stay the same. This was checked on 2,000 random histories, including duplicates,
missing and equal dates, and mixed naive, UTC and UTC+3 datetimes (`tests/test_lossless.py`).
A second independent review ran 30,000 of its own histories and found the same.

There are two recorded exceptions:

- **An inverted interval** (`invalid_at < valid_at`), which Graphiti never validates.
- **`expired_at` bookkeeping on a fact that arrives already ended**, when the filter leaves no
  candidates at all. This is the gap #1865 reports.

The claim assumes the model's choice about a fact does not depend on which irrelevant facts sit
beside it in the prompt.

**Negative controls.**

- The first version of this fix (cutoff = the new fact's own start) changes Graphiti's decision
  in 184 of 2,000. An independent Codex review caught it (DESIGN.md, mistake 5).
- The obvious fix, "search only facts still valid", changes it in 284 of 400 because it breaks
  back-filling.

**What fix 1 costs.** It makes the search cheaper. `bench/fix_cost.py` runs Graphiti's
invalidation-candidate search (hybrid, limit 10) on Neo4j: medians of 20 warm runs, a person with
H superseded homes moving once (`results/fix_cost.jsonl`).

| superseded homes | search, main | search, fix 1 | candidate text shown to the model, main → fix 1 |
|---|---|---|---|
| 0 | 24.6 ms | 24.6 ms | 20 → 20 characters |
| 10 | 32.6 ms | 30.6 ms | 199 → 20 |
| 200 | 48.8 ms | 25.8 ms | 204 → 20 |
| 1,000 | 113.5 ms | 32.0 ms | 204 → 20 |

**Why it is cheaper.** The vector channel filters before it scores, so stale facts are never
compared. The model sees the one fact it needs instead of ten stale ones.

**Facts about other slots do not crowd it out.** With 20 superseded homes and up to 30 still-valid
facts about the same person in other slots (hobbies), the current home was still among the
candidates in 10 of 10 runs. This uses the word-overlap embedder; a semantic embedder could rank
differently.

**Who is affected.** Production graphs churn: #1728's reporter measured 1,616 of 3,950 facts
(41%) with an end date, and another user 16%. The fault needs about 10 superseded facts in one
subject's history. An app that has called `Graphiti.search(num_results=k)` with k < 10 lowers that
to k, because the call overwrites the shared recipe (#1594); Graphiti's own e-commerce notebook
uses `num_results=2`.

**What fix 1 does not fix: back-fill at depth.**

The gate ingests random histories in random order and checks the rules any correct temporal
memory obeys (`graphiti_gate/properties.py`). 60 histories, 2-30 homes each, Neo4j:

| rule | homes | main | fix 1 |
|---|---|---|---|
| exactly one current home | 10-19 | 17/21 | **21/21** |
| exactly one current home | 20+ | 8/19 | **18/19** |
| every home ends where the next begins | 2-9 | 19/20 | 20/20 |
| every home ends where the next begins | 10-19 | 1/21 | 8/21 |
| every home ends where the next begins | 20+ | 0/19 | **0/19** |
| end dates never extended; re-ingesting changes nothing | all | 60/60 | 60/60 |

**One failing trial, traced** (`results/gate/traces/fix1-trial7-vilnius.txt`):

1. A home from 1907 (Vilnius) arrives 4th and is correctly ended at the earliest later home
   known then (1915).
2. The 1908 home (Kyoto) arrives 9th, so Vilnius should be shortened to 1908.
3. Vilnius ends after 1908, so fix 1 keeps it as a candidate.
4. But 8 of Alice's facts and 4 of another person's are all still relevant, the search returns 10,
   and Vilnius is the 11th.

Fix 1 removes candidates that cannot matter. It cannot help when the candidates that *can* matter
exceed the limit.

**Fix 3: add the subject's temporal neighbours** (`upstream/03-backfill-temporal-neighbors.patch`,
on top of fix 1; not proposed upstream). A back-filled fact needs exactly two facts to be right:
the subject's next fact after its start, which ends it, and the fact open at its start, which it
ends. So, besides the ranked search, Graphiti now asks the database for up to 3 of the subject's
facts starting after the new one and up to 3 open at its start, preferring the same relation name.
The model still decides; these are only extra candidates.

60 random histories (2-30 homes) plus 30 deeper ones (31-86 homes), Neo4j, oracle judge, on
Graphiti main `b7fc30f` (`results/gate/props-*`; regenerated by `bench/summarize.py`). The table
above was measured on the earlier main `5d47d4d`. Fix 1's counts differ between the two (8/21
there, 14/21 here at 10-19 homes); the runs differ in Graphiti version, the earlier run's seed is
not recorded, and crowded histories are not deterministic on their own (below), so the cause is
not separated:

| chronology rule | homes | fix 1 | fix 1 + top 30 | fix 1 + fix 3 |
|---|---|---|---|---|
| every home ends where the next begins | 10-19 | 14/21 | 21/21 | **21/21** |
| | 20-30 | 3/19 | 19/19 | **19/19** |
| | 31-60 | | 3/6 | **6/6** |
| | 61-90 | | 0/11 | **11/11** |

- **Raising the limit only moves the cliff.** Showing 30 candidates instead of 10 fixes histories
  up to about 30 facts and fails beyond them; fix 3 holds at every depth tried.
- **Nothing else changes.** All other rules pass. The gate's 34 scenarios show no pass/fail change
  (one row, `additive-over-eager`, is a known gap and fails in both). Graphiti's CI unit command
  gives 405 passes on main and 415 with fixes 1 and 3, the 10 extra being their new tests, with
  the same 5 files failing to load on both for missing optional clients (`results/unit-suite.txt`).
- **Both databases.** The regression test (`upstream/tests/test_temporal_regressions.py`, both
  neighbours worded so ranked search misses them) fails on main, #1596, #1957 and fix 1 alone, and
  passes with fix 3 on Neo4j and FalkorDB.
- **Cost** (`bench/neighbors_cost.py`, the patch's own function, history back-filled in the
  middle): 29, 47 and 75 ms at 10, 100 and 1,000 facts on Neo4j; 12, 40 and 65 ms on FalkorDB.
  Reading the subject's whole history instead costs 265 and 606 ms at 1,000 facts.
- **The trade-off: more candidates, more exposure to a wrong model.** One synthetic relation with
  many true values (12 open "likes", one back-filled; `bench/multivalued.py`, 20 trials): with a
  correct judge, nothing is ended in either version. With a judge that calls every shown fact a
  contradiction, fix 3 wrongly ends 5.90 of the 12 existing likes per episode against 5.65 with
  fix 1 alone, and ends the new like in 20/20 trials against 16/20.
- **Same facts, same history.** When candidates are crowded, which facts win the 10 places is
  not stable from run to run, so the same input can be stored differently. The cause is not
  isolated. A review that pinned every node and edge id still saw results vary (its scripts are
  not saved in this repository), so id order alone does not explain it. `bench/determinism.py` ingests 20 crowded back-fill histories (`--crowded` in the
  fuzzer) 4 times through `resolve_extracted_edges` and 4 times through `add_triplet`, oracle
  judge, Neo4j (`results/determinism-*.jsonl`):

  | | back-filled home always ends where the next begins | one outcome over 8 runs | both paths agree |
  |---|---|---|---|
  | main | 0/20 | 13/20 | 13/20 |
  | fix 1 | 10/20 | 20/20 | 20/20 |
  | fix 1 + fix 3 | **20/20** | **20/20** | **20/20** |

  On main, 7 of 20 histories were stored with different end dates on different runs of identical
  input; with fix 1 the answer is stable but wrong in half; with fix 3 it is stable and right.
  "Both paths agree" is a consistency check, not a correctness one: with fix 1 the paths agree in
  20/20 while 10/20 are right.
- **One episode, two old homes: not fixed, and an open question.** `add_episode` resolves an
  episode's edges in one batch, and edges of one batch do not see each other; Graphiti only
  removes exact duplicates within a batch (`edge_operations.py`, the fast path at the top of
  `resolve_extracted_edges`) and leaves changes inside one message to the extraction model, whose
  prompt asks it to set `invalid_at` "if a change or end is expressed". `bench/batch.py` adds two
  old homes, adjacent in time, to a crowded history, with no end date given (20 seeds, oracle):

  | | one at a time | in one episode |
  |---|---|---|
  | main | 3/20 | 0/20 |
  | fix 1 | 20/20 | 0/20 |
  | fix 1 + fix 3 | 20/20 | 0/20 |

  The batched arm is wrong in all 20 on every version. In the 2 seeds where the two are the
  latest homes, both stay open: two current homes from one message. In the rest, both are ended,
  at the wrong dates.

  **With a real model it does not happen, so it is not a fault.** `bench/realistic.py --episode
  two-moves` sends "In Y Alice moved to Bergen, and in Y+1 she moved again, to Beirut." through
  the full `add_episode` with `gpt-5.6-luna`, 30 seeds per arm: the model ended Bergen during
  extraction in 30/30 on main and 30/30 with fixes 1 and 3. Every extra open home in those runs
  was the depth fault F1 (main left an old home open in 21/30, fixes 1 and 3 in 9/30).
- **Usable today.** `fix.apply_invalidation_filter(); fix.apply_backfill_neighbors()` installs
  fixes 1 and 3 at runtime. On unpatched main, with them installed, the regression tests give the
  same results as the patches: depth and back-fill tests pass on Neo4j and FalkorDB, and the
  OR-ed date tests still fail (`bench/plugins/runtime_fixes.py`, `results/runtime_fixes_pack.txt`).
- **Fix 3 with a real model, and what it changed.** The first real-model run of fix 3 (v3,
  `results/realistic/d20-gpt-5.6-luna-fix1+3-v3.jsonl`), on the same 30 seeds as fix 1 alone and
  an ordinary move, did worse: the current home was retired in 25/28 against 30/30 (2 runs
  errored in Codex), 3 seeds failed only with fix 3 and none only without it, and tokens rose.
  The cause was in fix 3's design: when no fact of the same relation was nearby, it fell back to
  the subject's other relations, and in this world Alice's job started after her move, so every
  ordinary move looked like a back-fill and the model was shown her job and hobby as candidates.
  Fix 3 now acts only on a back-fill, judged by a later fact with the same relation name, and
  never shows other relations (`tests/test_fix.py`, the upstream patch's tests). An ordinary
  update is then exactly fix 1. The cost: when the model names the relation differently (say
  `MOVED_TO` against `LIVES_IN`), fix 3 does nothing and fix 1's behaviour remains. The real-model
  run of this version is waiting on Codex credits; every other fix 3 number above was re-measured
  on it.
- **How a review changed it.** The first version asked FalkorDB for "started at or before" with
  `<=`. On FalkorDB 4.10.3, a range index makes a string `<` or `<=` return every row; 6.0.1 is
  correct (`bench/falkordb_range_index.sh`, `results/falkordb_range_index.txt`). A Sonnet review (standing in for Codex while its credits were out) found it. That check
  now runs in Python. The first regression test missed it, because it only exercised the "later"
  side. The deep fuzzer missed it too (`results/fuzz/run-4-*`): deep runs check rules per
  database rather than comparing databases, and their histories rarely crowd out the fact open at
  the start. The test now forces both sides.

**Status.** Not reported upstream as of 2026-10-04: our searches of issues and PRs found nothing
on candidate crowding. #1728 and its PRs cover the opposite error, wrongly retiring facts.

**Interaction with #1594.** `Graphiti.search(num_results=k)` overwrites the shared recipe's limit
(`graphiti.py:1631`; the fix is open in PR #1594). After one `search(..., num_results=2)`, as in
Graphiti's e-commerce example, the invalidation pool is 2 and this fault starts at 2 past values
instead of 10. Our benches never call the public search before measuring.

---

## F2. Fulltext search applies the result limit before search filters

**Intent: unintended.** A filtered search can return nothing although matching facts exist.

**Mechanism.** `edge_fulltext_search` asks the Neo4j fulltext index for its top `2 x limit` hits
(`{limit: $limit}`). Every `SearchFilters` condition is applied after that
(`search_utils.py:224-290`). The conditions affected are date filters, `edge_uuids`, `edge_types`
and node labels. With a filter, the index can hand back only rows the filter rejects.

**Evidence.** An "as of T" search uses the date filter the maintainers recommend (#769), with 20
seeds.

| past homes | search | no fix | fix 2 |
|---|---|---|---|
| 50 | fulltext only, "now" | 7/20 [0.18, 0.57] | 20/20 |
| 50 | fulltext only, "as of past" | 9/20 [0.26, 0.66] | 20/20 |
| 50 | hybrid (default), either | 20/20 | 20/20 |
| 0-20 | any | 19-20/20 | 20/20 |

**Smaller than first thought, and why.** The default hybrid search is unaffected: its vector
channel filters before it limits (`neo4j/operations/search_ops.py:287-330`). The fault hits
fulltext-only searches. It also hits any search where the vector score falls below the 0.6
floor, for example "Alice moved to Berlin" against "Alice lives in Tokyo" with a lexical embedder.

**Fix: not proposed.** `upstream/02-*.patch` (no index limit when filtered) is correct but costs
20-80x on large groups:

| matching facts | now | patch 02 |
|---|---|---|
| 10,000 | 26 ms | 530 ms |
| 50,000 | 29 ms | 2,411 ms |

The cost is Graphiti's per-hit `MATCH (n)-[e {uuid: rel.uuid}]->(m)`, not Lucene. At 50,000
facts that part is 2,497 ms, against 164 ms for the index alone
(`results/fulltext_cost_breakdown.txt`). Two cheap designs remain, and the choice belongs to the
maintainers:

- **Reuse the yielded relationship,** as Graphiti already does for FalkorDB and BFS: 356 ms.
- **Over-fetch adaptively:** 46 ms when 200 hits suffice.

**Status.** No dedicated issue. PR #1940's description mentions the `edge_uuids` case in passing.

---

## F3. A restated fact with an end date can lose that end date

**Intent: unintended.** Status: adjacent to open work (#1841 and PR #1940, #1865 and PR #1867).

**Mechanism.** Take "Alice worked at Acme until 2023" arriving after "Alice works at Acme". If
the model calls them duplicates, Graphiti keeps the old, open edge (`edge_operations.py:745-748`)
and drops the new end date. If it calls them a contradiction, nothing is closed when both start
the same day (`resolve_edge_contradictions`, `valid_at` ties).

**Evidence.** Reproduced with a scripted model in the first audit (Kuzu, in memory); it has not
been measured on Neo4j or with a real model.

**Not pursued.** The area is being changed by two open PRs. Recorded so the gate can test it.

---

## F4. Search filters with more than one OR-ed date range use the wrong dates (known: #488)

**Intent: unintended.** Status: reported as #488 and fixed by open PR #1596 (unmerged since June
2026). Not a new finding: the gate already had a scenario for it (GATE.md, open-queue table), and
the fuzzer's OR-ed date search was written knowing it. What the fuzzer added is breadth: the bug
shows on Neo4j, FalkorDB and Kuzu, in every random history. Evidence posted on #1596.

**Mechanism.** `edge_search_filter_query_constructor` names each date parameter by its position
inside its own OR group only (`search_filters.py:157`, `'valid_at_' + str(j)`; the same for
`invalid_at`, `created_at`, `expired_at`). A second group overwrites the first group's value, so
every group is compared against the last date. Filters whose other groups hold only `IS NULL`,
such as the usual "valid now" filter, are not affected. Neither is the MCP server, which builds
single ranges (`mcp_server/src/utils/type_config.py:149`).

**Evidence.** Shrunk to one fact and one search: a fact starting 1997-01-01, filtered
`valid_at=[[< 1997-01-01], [> 1998-01-01]]`, is returned on Neo4j, FalkorDB and Kuzu
(`graphiti_gate/fuzz_cases/`). Across 30 random histories × 3 databases: 162 wrong results on
main `b7fc30f`, 0 with #1596 merged onto it (`results/fuzz/run-2-main.jsonl`,
`run-2-pr1596.jsonl`).

---

## Not faults

- **`lucene_sanitize` escaping the letters O, R, N, T, A, D** (#1302, PRs #1569 and #1595).
  - **Label: not a fault** (a simplification of operator escaping).
  - **What Lucene does.** Lucene 9.11's classic `QueryParser`, behind Neo4j fulltext, parses
    `EBI\T\D\A` to the term `ebitda`, the same as `EBITDA`. The only effect is that bare `AND`,
    `OR` and `NOT` become words, which is the intent (`bench/lucene/run.sh`).
  - **What this means for #1302.** The issue's "No BM25 match" is not what Lucene does.
- **Node summaries overwritten by new episodes** (#1166).
  - **Label: not a fault.** A maintainer states that summaries are intended to be mutable.
- **graphiti-core fails to import on a fresh install (`httpx`).**
  - **Label: unintended, already reported** (#1893). We pin `httpx` as a workaround.

---

## How this was checked

- **Independent review.** A second model, Codex `gpt-6-astra` at high effort, was asked to prove
  each claim wrong by running its own code.
  - **Round 1** found the duplicate-path error in F1's first fix, plus three runtime-patch
    problems; all are fixed and covered by tests.
  - **Round 2** confirmed the corrected cutoff on 30,000 histories. It found a second exception
    (the no-candidate branch), a collision in the runtime wrapper, and an untested call site;
    all three are fixed or documented (DESIGN.md, mistake 7).
  - **Round 3** reviewed the gate built on these findings. It found an evaluator that could pass
    corrupted graphs, a mutant wrongly called equivalent, and wrong answers to old Graphiti
    versions. All are fixed and the affected measurements were rerun (DESIGN.md, mistake 11).
    It confirmed the history result: the depth fault predates #906.
- **Mistakes in this repository's own harness.** Concurrent runs shared a graph, and a bench
  called the unpatched function. Both were caught; the affected results were deleted or moved to
  `results/superseded/` and rerun (DESIGN.md).
- **Not measured.**
  - FalkorDB, Kuzu and Neptune.
  - Semantic embedders: runs use a lexical hash embedder, so they need no model or network.
  - Real-model runs beyond 12 seeds per arm.
