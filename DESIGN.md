# Design notes

How each measurement in this repository was built, why it was built that way, and the mistakes
made along the way. Every number in the README comes from a script in `bench/`, against
graphiti-core at commit `4f98b7c` (0.30.2) and Neo4j 5.26.2, the version in Graphiti's own CI.

## Isolating retrieval from the model: the oracle

Graphiti decides whether a new fact retires an old one in two steps. A search picks up to 10
candidate facts, then the LLM says which of them the new fact contradicts. A wrong answer can come
from either step. To measure the search alone, `graphiti_temporal.testing.OracleLLM` answers
Graphiti's own `dedupe_edges.resolve_edge` prompt from ground truth. It parses the candidate lists
out of the real prompt and marks every candidate about the same subject and slot with a different
value. When the oracle fails to retire a fact, the only possible cause is that the fact was not in
the prompt. `bench/history_depth.py` records both whether the live fact was shown and whether it
was retired, and the two columns agree in every run.

The real-model runs (`bench/real_llm.py`) then put Codex in the oracle's place. They send the full
`add_episode` pipeline (extraction, entity resolution, edge resolution) through `codex exec`.

## The world

`graphiti_temporal.world` builds a person who moved home N times. Each past home is a fact with
`valid_at`, `invalid_at` and `expired_at` set, which is how Graphiti stores a superseded fact.
The current home is open. Three other people add unrelated history in the same group.

- **Same wording.** Every fact uses "<person> lives in <city>", so the live fact is no easier or
  harder to retrieve than the history behind it. The first audit script phrased the live fact
  differently ("currently resides at"). That flatters nothing, but it confounds wording with
  validity, so it was dropped.
- **Shuffled insertion order.** Order is shuffled per seed. Equal scores are otherwise broken by
  storage order, and a fixed order would decide the result.
- **Different target entities.** The first audit put every edge between the same two nodes. In
  Graphiti, "Alice lives in Paris" and "Alice lives in Berlin" link Alice to different city
  nodes. So the only way the old home reaches the model is the global invalidation search, not
  the same-endpoint lookup. The world uses separate city nodes.
- **Embeddings.** Embeddings are a bag-of-words hash (`hash_embedding`), so runs need no model and
  no network. Facts that share words are similar; nothing else is. This understates what a
  semantic embedder knows, which is why the real-model runs exist.

## Why the filter is safe: "lossless", and how that was checked

Fix 1 keeps out of the candidate search any fact whose `invalid_at` is at or before a cutoff.
The cutoff is the earliest `valid_at` among the new edge and its duplicate candidates. There is no
filter at all when the new edge has no `valid_at`.

**Why a fact before the cutoff can be dropped.** The edge Graphiti goes on to resolve is either the
new edge or a duplicate the model picks (`edge_operations.py:745-748`). So its start is no earlier
than the cutoff. A fact that ended at or before that start cannot be retired by it:
`resolve_edge_contradictions` skips it (lines 553-556). Nor can it end the resolved edge: only a
candidate starting after the resolved edge can (lines 826-840), and a well-formed interval that
starts later also ends later, so it survives the filter.

**Why there is no filter when `valid_at` is missing.** In that case Graphiti may ask the model for
the dates after the search (lines 812-813), so no cutoff is known in advance.

**What "lossless" means here, precisely.**

- **Always the same:** the resolved edge, its `valid_at` and `invalid_at`, and the set of retired
  facts with their end dates.
- **Also the same, unless the filter leaves no candidates at all:** `expired_at` and attributes.
- **The assumption.** The model's choice about a fact does not depend on which irrelevant facts
  sit beside it in the prompt. A real model sees fewer, more relevant candidates; that is the
  point of the fix, and the real-model arms measure its effect directly.

`tests/test_lossless.py` checks this on 2,000 random histories through Graphiti's real
`resolve_extracted_edge`. The test covers:

- 0-3 duplicate candidates, of which the model picks some,
- 1-11 invalidation candidates,
- missing, equal and same-day dates,
- naive, UTC and UTC+3 datetimes mixed together,
- dates the model reports after the search.

A second independent review ran 30,000 histories of its own and found no change to the resolved
edge, its dates or the retired set.

**Negative controls.** The test can tell a correct filter from two plausible wrong ones:

- **The first version of this filter** (cutoff = the new edge's own start) changes the decision in
  184 of the 2,000.
- **"Only search facts that are still valid"** changes it in 284 of 400 histories of the earlier,
  simpler generator, because it breaks back-filling.

**Two exceptions, recorded rather than hidden.**

1. **An inverted interval** (`invalid_at < valid_at`) can end the new fact and is dropped by the
   filter. Graphiti never validates intervals, so such a fact can exist
   (`test_inverted_interval_is_the_one_exception`).
2. **When the filter leaves no candidates**, Graphiti takes its no-candidate branch
   (`edge_operations.py:653`), which skips `expired_at` bookkeeping and attribute clearing. A new
   fact that arrives already ended then keeps `expired_at` empty
   (`test_no_candidate_branch_skips_expired_at_bookkeeping`). Its dates are right. The branch's
   inconsistency is the one #1865 reports, and PR #1867 addresses it. In the 2,000 random
   histories the filter empties the list 14 times, and none of those differs, because the
   difference needs a fact that is already ended on arrival.

## Fix 2 and its cost

The fulltext channel calls `db.index.fulltext.queryRelationships(..., {limit: $limit})` and
applies `SearchFilters` afterwards (`search_utils.py:224-290`). With any filter, the index can
hand back only rows the filter rejects. `upstream/02-*.patch` drops the index limit whenever a
filter is present, and the query's own `LIMIT` still bounds the result. It is correct: a
fulltext-only "as of" search at 50 past homes goes from 7/20 to 20/20.

**It is also too slow to propose as written.** `bench/fulltext_cost.py` times one group whose
facts all match the query, 1 in 10 still valid, with the median of 15 warm runs on Neo4j 5.26.2:

| matching facts | index limit (now) | no index limit (patch 02) |
|---|---|---|
| 10,000 | 26 ms, 2 valid rows of 20 | 530 ms, 20 of 20 |
| 50,000 | 29 ms, 2 valid rows of 20 | 2,411 ms, 20 of 20 |

**Where the time goes.** A breakdown at 50,000 facts:

| query part | time |
|---|---|
| raw index hits alone | 167 ms |
| hits plus Graphiti's per-hit `MATCH (n)-[e {uuid: rel.uuid}]->(m)` | 2,411 ms |
| the yielded relationship used directly | 354 ms |
| an over-fetch of 200 | 31 ms |

The cost is the re-`MATCH`, not Lucene. Graphiti already removed that pattern for FalkorDB
(`search_utils.py:207-217`) and for BFS (`tests/utils/search/test_edge_bfs_query_shape.py`).

**Decision.** A correct and cheap fix needs a choice that belongs to the maintainers: use the
yielded relationship on Neo4j, or over-fetch adaptively until enough rows pass the filter. So
fault 2 is reported as a finding with these measurements, not proposed as a patch.

**Why it matters less here.** The real-model runs below show fix 1 alone is enough for
invalidation. The first 1,000-fact timing (7.8 s) was a warm-up artefact. A clean rerun in the
breakdown gives 56 ms.

## Mistakes made here, and how they were caught

1. **Two benchmarks shared a graph.**
   - **What happened.** Group ids were derived from the seed. The as-of benchmark and the
     history-depth benchmark, and later two real-model arms, ran at the same time with the same
     seeds, so they wrote into and deleted from the same groups.
   - **How it was caught.** A real-model trial crashed with `EdgeNotFoundError`: another run's
     cleanup had deleted its edge.
   - **The fix.** Group ids now carry a random suffix (`world.build`).
   - **Results.** Every result from overlapping runs was deleted and rerun. The first baseline
     history-depth sweep had run alone; it is kept as `history_depth_fix-none.first-run.jsonl`
     and agrees with the rerun.
2. **The runtime fix was silently not applied.**
   - **What happened.** The first "fixed" sweep showed 5.0 expired facts in the candidate pool at
     history 5, where the fix should give 0. The bench had imported `resolve_extracted_edges` by
     name before `fix.apply()` replaced it.
   - **The fix.** The bench now calls it through its module. `fix.py` documents the trap.
   - **What stops it recurring.** The "expired facts in pool" column exists to catch this.
3. **A fault from the code audit was smaller than claimed.**
   - **What was claimed.** The first audit (Kuzu, in memory) reported that a current-only search
     returns nothing once history fills the limit.
   - **What Neo4j showed.** On Neo4j the default hybrid search is unaffected: the vector channel
     filters before it limits, and each channel fetches `2 * limit`. Only fulltext-only searches
     lose the fact (5 of 20 at 50 past homes).
   - **Why it still matters.** Fix 2 still matters for the real pipeline, where the new fact's
     wording ("Alice moved to Berlin") can leave the vector channel below its 0.6 similarity
     floor, and for the `edge_uuids` filter Graphiti uses to find duplicates.
4. **A claimed fault was not one.**
   - **The claim.** `lucene_sanitize` escapes the letters O, R, N, T, A and D (#1302), said to
     break BM25 for words like "EBITDA".
   - **What Lucene showed.** Lucene 9's `QueryParser` turns `EBI\T\D\A` back into the term
     `ebitda`. The only effect is that bare AND, OR and NOT become plain words, which is the
     intent.
   - **Status.** Not a fault; it is recorded in FINDINGS.md and is not this repository's subject.
5. **The first "lossless" claim was wrong, and an independent review caught it.**
   - **The miss.** Codex (`gpt-6-astra`, high effort) was asked to break every claim by running
     code. It found that when the model marks the new fact as a semantic duplicate of an older
     one, Graphiti continues with the duplicate's earlier `valid_at`. A cutoff taken from the new
     fact alone can then drop a candidate the duplicate would have retired.
   - **Its counterexample.** A duplicate from Jan 6, a contradicting fact from Jan 1 to 11, and a
     new fact on Jan 21: the outcome changed in 360 of its 2,000 random cases. The original test
     never produced duplicates, so it could not see this.
   - **The fix.** The cutoff now includes the duplicate candidates, and the test now covers the
     duplicate path and mixed timezones. The first version fails the new test in 184 of 2,000.
   - **What else the review found, also fixed.**
     - The runtime patch keyed dates by fact text alone, so two edges with the same wording
       collided. It now uses the earliest start per (text, group), or no filter if any is undated.
     - Naive datetimes were not normalized to UTC.
     - The runtime interception now also checks the group.
   - **What the review confirmed.** The fault, the 284/400 control, the Lucene result, and that
     bulk ingestion and `add_triplet` are covered.
   - **Results.** Results measured with the first version are kept under `results/superseded/`;
     the fixed arms were rerun.
6. **Graphiti's public search mutates the shared recipe.**
   - **The bug.** `Graphiti.search` sets `search_config.limit = num_results` on the module-level
     `EDGE_HYBRID_SEARCH_RRF` (`graphiti.py:1631`), which the invalidation search also uses. One
     `search(..., num_results=2)`, as in Graphiti's e-commerce example notebook, shrinks every
     later invalidation pool to 2.
   - **Status.** This is already reported, with a fix in open PR #1594. It is not our finding, but
     it moves the history-depth cliff, which is why the benches never call the public search
     before measuring.
7. **The second review found what the first fix still got wrong.**
   - **What it confirmed.** Running 30,000 histories of its own, Codex found the corrected cutoff
     never changes the resolved edge, its dates or the retired set.
   - **The overstatement.** It showed that "the one exception" was too strong: the no-candidate
     branch is a second one.
   - **The runtime wrapper error.** The wrapper pooled the dates of two extracted edges that share
     text and group. One edge's duplicate could then lower the other's cutoff and, under the
     10-result limit, push its live fact out. The upstream patch does not do this, because it
     filters per edge. The runtime version now leaves such edges unfiltered, exactly as Graphiti
     does today.
   - **The test gap.** It found that no upstream test caught the call site passing `[]` instead
     of the duplicate candidates. One now does: it fails with that regression and passes with the
     fix.
8. **A scenario that could not fail.**
   - **The miss.** `dedup/duplicates-between-entities` asserted only that the incoming fact
     stayed open. Under the mutant it targets, the fact was wrongly merged into another entity's
     edge, and the merged edge is open, so the scenario passed.
   - **How it was caught.** Every new scenario must pass on main and fail under its mutant; this
     one did not.
   - **The fix.** A `merged: false` expectation.
9. **An argument for "equivalent mutant" that a measurement disproved.**
   - **The argument.** Two boundary mutants (`<=` to `<`) looked unable to change any outcome,
     and the reasoning seemed sound.
   - **What the measurement showed.** `bench/equivalence.py` ran 20,000 histories. One mutant
     changes the list Graphiti returns but never what it stores, so it is equivalent for any
     graph-based test. The other is equivalent only for well-formed intervals. A third mutant,
     assumed similar (`retire-same-start`), changes the stored graph in 9,394 of 20,000
     histories, and neither suite caught it in round 1.
   - **The lesson.** "Equivalent" is now a measured label, never an argued one.
10. **A network error reported as a merge conflict.**
    - **What happened.** The reach run reported PR #1729 as conflicting, but GitHub's `git fetch`
      had failed transiently ("HTTP2 framing layer"). The triage had merged it cleanly.
    - **The fix.** Merge conflicts now have their own exception (`MergeConflict`), and every
      other failure surfaces as an error. `main` is fetched once per run with retries, so every
      revision in a run shares one base.
11. **The third independent review found the gate's own evaluator too lenient.** Codex
    (`gpt-6-astra`, high effort) was asked to break the gate's claims. It found five errors.
    - **Corrupted graphs could pass.** The evaluator checked only the facts a scenario named, so
      an extra duplicate edge, a changed fact text or start date, or a merged fact also stored on
      its own all passed. GATE.md claimed "any unexpected change fails", which was false. The
      evaluator now accounts for every stored edge and every field of every unmentioned fact.
      `tests/test_gate_evaluator.py` holds the three corrupted graphs, which must fail. On main
      the stricter evaluator gives the same verdict on all 29 scenarios.
    - **An "equivalent" mutant was not equivalent.** The equivalence generator stamped every
      ended fact as expired, which hid the ended-but-unexpired state (#1865's gap). With that
      state allowed, `skip-ended-boundary` changes the stored graph in 838 of 20,000 histories.
      The comparison now uses full per-edge state (text, endpoints, interval, expiry, episodes);
      resolvers that corrupt provenance or endpoints are detected in 2,000 of 2,000.
    - **The judge gave wrong answers to old Graphiti.** Old versions number each list from 0 and
      read contradictions against the candidate list only, so the judge could make them retire
      the wrong fact. A fact containing tag text could also break the parser. Both are fixed and
      tested. The #906 history result does not depend on either, and the review confirmed it
      independently.
    - **"Caught" was defined differently for the two suites.** For the gate, any verdict change
      counted, including a scenario that started passing. It now mirrors their suite: a passing
      scenario must fail or error. Flaky and improved outcomes are recorded but not counted, and
      a rule counts only in a history-size bucket of 5 or more trials where the base never fails.
      Rounds 1 and 2 used the old definition; round 3 reruns every mutant under the new one.
    - **Smaller fixes.**
      - The live cache key could collide when the same text was split differently into
        messages; it is now structured. The run reported in GATE.md used the old keys, and those
        answers are kept but no longer replay.
      - The Fisher test's absolute tolerance distorted very small p-values. The reported
        p-values were unaffected; it now matches exact rational arithmetic.
      - Reach counted an imported function as run, and missed changes inside multi-line strings.
        It now maps each changed line to its statement, and counts a function only if its body
        ran.
    - **What the review confirmed.** The arithmetic of every reported mutation count, the #906
      result, and all 2,018 tests.

12. **A rediscovered bug was presented as a discovery.** The fuzzer's first finding, the OR-ed
    date filter, was described as found "with no prior knowledge". The scenario set already
    covered it (#488, fixed by open PR #1596), and the fuzzer's OR-ed search was written knowing
    it. A review by gpt-6-astra caught the claim; README, GATE.md and FINDINGS.md now call it a
    confirmation on three databases.
13. **The gate could not fail a CI job.** `diff` printed regressions and exited 0, a worker that
    crashed after some scenarios was accepted, and the fuzzer reported findings before seeing them
    twice. The same review found all three. Each now fails visibly, with tests
    (`tests/test_gate_ci.py`) that fail without the change.
14. **A FalkorDB bug looked like a Graphiti one, and its first explanation was wrong.** In the
    regression pack, the OR-ed date test failed on FalkorDB even with #1596. A raw query showed
    FalkorDB 4.10.3 returning an edge that matches neither range. The first explanation was that
    deleted edges leave stale index entries. A later minimal case, with no deletions, showed the
    real cause: with a range index on a string property, `<` and `<=` return every row as soon as
    any value lies below the bound. 6.0.1 is correct, and Graphiti's CI uses `falkordb:latest`, so
    nothing was reported. The pack starts each revision on a fresh FalkorDB graph, which avoided
    the bug only because a fresh graph has no lower values.
15. **A results file was about to be overwritten again.** `graphiti-gate props` names its output
    by revision only, so a second run on the same patch replaces the first. The first run's data
    was copied aside before the deep run finished (`props-*-max30.json`). Output names now include
    the run settings (`props-<revision>--t60-h30-s0.json`). A final check found two committed
    results overwritten by tonight's reruns anyway (`props-main.json` and a gate run); both were
    restored from git, tonight's copies kept under new names, and no result file is ever replaced
    now: a rerun gets `-run2`, `-run3` (`tests/test_gate_ci.py`).
16. **A formatter changed a file outside the patch.** Running ruff on Graphiti's whole package
    reformatted `llm_client/client.py`. It was caught in `git diff --stat` before the patch file
    was used, and reverted.
17. **Fix 3's first version was wrong on FalkorDB.** It asked the database for facts open at the
    new fact's start with `valid_at <= $start`, which mistake 14's FalkorDB bug turns into "every
    fact". A Sonnet review found it by reading the query and reproducing it; neither the
    regression test (which only covered the "later" side) nor the deep fuzzer did. Deep runs
    check rules per database instead of comparing databases, and their histories rarely crowd out
    the fact open at the start. A `--crowded` fuzz mode now builds that case on purpose; it flags
    the first version on FalkorDB in 8 of 20 seeds and the final version in none. The check now
    runs in Python, the test covers both sides, and the fuzzer gained `--varied` wordings.
18. **A cost was measured on the wrong query.** `bench/neighbors_cost.py` first timed a simpler
    one-sided query (about 17 ms at 1,000 facts), and that number was used to choose the design.
    The same review timed the real function at about 80 ms. The bench now times the patch's own
    function on both databases, and FINDINGS quotes that.
