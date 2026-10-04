# graphiti-gate: what a change does to Graphiti's memory, in about a minute

Graphiti's tests check functions in isolation. Nothing starts from a graph, sends a new fact in,
and checks what the graph believes afterwards. So nobody can cheaply answer "does this PR fix what
it claims, and what else does it change?" for the logic that decides which facts are current.

The gate answers that for any revision: `main`, a PR merged onto main, a patch, or a local
checkout. It needs Neo4j and no model or API key.

```bash
graphiti-gate diff main pr:1729          # scorecard: fixed / broke / unchanged, and why
graphiti-gate triage pr:1729 pr:1940 ... # a PR queue in one table, with how much of each PR it ran
graphiti-gate props main pr:1867         # rules on random histories, rates by history size
graphiti-gate audit                      # damage in a live graph (read-only, proposes repairs)
graphiti-gate live pr:1940 --repeats 10  # a real model (Codex) as the judge: pass rates vs main
python -m graphiti_gate.bisect S.yaml --good A --bad main  # the commit that changed a scenario
```

## What it checks

- **29 scenarios** (`graphiti_gate/scenarios/`), in seven families: history depth, over-invalidation,
  intervals, duplicates, search filters, entity resolution, bulk ingestion. Each holds a starting graph, incoming facts, and
  the graph expected afterwards, and names its source: an issue, a PR, or a line of Graphiti's
  code.
  - **Every fact is checked**, including facts the scenario does not mention: any unexpected
    change to them fails it.
  - **Expected-to-fail and disputed cases.** `known_gap` marks outcomes Graphiti does not reach
    yet. `disputed` cases are shown but never counted; for example #1728's "additive" case, where
    maintainers have not ruled.
- **Judges that make real models' mistakes** (`graphiti_gate/judges.py`):
  - `oracle`: answers from ground truth,
  - `over_eager`: flags unrelated facts, as in #1728,
  - `under_resolving`: stays silent, as #1772 measured on deepseek,
  - `scripted`.

  The judge reads the facts out of whichever prompt the revision sends, and answers in its id
  format: integers on main, `"E0"`/`"I2"` in #1772.
- **Rules on random histories** (`graphiti_gate/properties.py`). A random history of moves is
  ingested in random order, one episode at a time, and must satisfy:
  - exactly one current value,
  - every interval ending where the next begins,
  - no end date ever moved later,
  - re-ingesting a fact changing nothing.
- **Reach** (`graphiti_gate/reach.py`): which of the statements a PR changes in `graphiti_core`
  the gate actually executed. Then "no change" on a PR the gate never ran reads **untested**, not
  pass.
- **Determinism.** Every scenario is built tie-free and runs several times. A verdict that varies
  is reported as `flaky`, never as pass or fail.

## Does it catch bugs? Measured against Graphiti's own tests

We injected small, plausible bugs into Graphiti's resolution code (`graphiti_gate/mutants.py`):
a flipped date comparison, a dropped `expired_at` stamp, an off-by-one in candidate ids, a halved
search limit. Each was run against Graphiti's own CI tests and against the gate. Graphiti's tests
are the unit job plus the database job's Neo4j tests: 500 pass on clean main.

**How "caught" is defined.** It is defined the same way for both suites.

- **Their tests:** a test that passes on main fails.
- **The gate:** a scenario that passes on main fails or errors.
- **Not counted for the gate:** scenarios that start passing, and flaky outcomes. Both are
  recorded in the results.
- **Random-history rules:** a rule counts only in a history-size bucket of at least 5 trials
  where main never fails.

**Round 3 results** (`results/gate/mutation-round3.jsonl`). Round 3 is the corrected gate: 29
scenarios, an evaluator that checks the whole stored graph, and the definition above.

| Bugs | Graphiti's tests | The gate | Either |
|---|---|---|---|
| Design set: 14 bugs | 3 / 14 | **12 / 14** | 13 / 14 |
| **Held out: 6 bugs never used to design a scenario** | **1 / 6** | **4 / 6** | 4 / 6 |

**How to read it.**

- **The held-out row is the number to trust.** The design set is flattered: scenarios were added
  after earlier rounds showed what they missed. The held-out list was recorded before that, with
  a hash (`results/gate/held_out_commitment.json`). The hash proves the list has not changed since
  it was recorded. Its timestamp is self-reported, so nothing here proves independently when that
  was.
- **One mutant is excluded as equivalent, by measurement.** `skip-new-ended-boundary` changes no
  stored field of any edge in 20,000 random histories (`bench/equivalence.py`, which compares text,
  endpoints, interval, expiry and episodes). Resolvers that corrupt provenance or endpoints are
  detected in 2,000 of 2,000. A second mutant, `skip-ended-boundary`, was first thought
  equivalent; the third review showed it changes the stored graph when an ended fact was never
  stamped expired. It is a real bug, and both suites miss it.
- **The suites catch different things.** Graphiti's unit tests catch dedup bookkeeping the gate
  does not assert, such as which of two equal duplicates wins; that depends on search ranking, and
  asserting it would make the gate flaky. The gate catches the temporal logic their tests never
  reach. Together they catch 13 of 14.
- **The held-out misses are real gaps.** No scenario sends two same-text facts in one batch, and
  one mutant may be equivalent but was not measured.
- **Earlier rounds.** They used a looser evaluator and counted any verdict change for the gate.
  They gave the same held-out result, 4 of 6 against 1 of 6 (`mutation.jsonl`,
  `mutation-round2*.jsonl`; DESIGN.md, mistakes 9 and 11).

## Real past bugs: what the gate would and would not have caught

Injected bugs are graded by the person who wrote the tool, so the gate was also run against
Graphiti's own history. We took 13 fixes merged since Sept 2025 that touch the code the gate
exercises (`bench/real_regressions.py`, `results/gate/real-regressions.jsonl`). The 29 scenarios
were frozen and none was written for these bugs. Each fix F was tested on its parent and on F.

**The gate would have caught 0 of the 13 before they shipped.** The fixes are about robustness to
malformed model output (#939, #965, #968), edge-type validation (#948), attributes (#1242, the
Aug 2026 node-attributes fix) and summaries (#1223). That is a different class of bug from the
behaviour the gate checks. Every old version runs cleanly: 0 errors after the judge learned to
answer entity-resolution prompts in each version's schema.

**It did find a regression in the other direction.** The scenario for #1734 (an entity with the
exact name, below the embedding threshold) was written from the issue text. It **passes** on
Graphiti from October 2025 and fails on today's main. `python -m graphiti_gate.bisect` searched
86 commits in 14 steps:

- **Last good:** `7d65d5e` (2026-03-11).
- **First bad:** `c4e6923`, "Upstream Zep internal improvements" (#1361, 2026-03-31).

That commit replaced the hybrid candidate search in `_collect_candidate_nodes` with a vector-only
search (`node_similarity_search`, cosine >= 0.6). The diff confirms it (`results/gate/bisect-1734.txt`).
So #1734 is a regression, and #1741 restores what the hybrid search used to do. Whether the switch
was a deliberate trade-off cannot be told from the diff.

## A real model as the judge

`graphiti-gate live` puts Codex in place of the judge personas: it answers each revision's own
prompts, 10 times per scenario. Every answer is recorded under a hash of its exact prompt, so a
rerun costs nothing, and a revision whose prompts changed shows up as cache misses. The run below
was Codex `gpt-5.6-luna` at low effort, 240 calls, in `results/gate/live-1.md`.

| scenario | main | #1772 | #1940 |
|---|---|---|---|
| #1841: a release ends the assignment | 0/10 | 0/10 | **10/10** (p < 0.001) |
| history depth (fix 1's fault) | 0/10 | 0/10 | 0/10 |
| #1865: a fact true again | 0/10 | 0/10 | 0/10 |
| #1728: an unrelated fact | 10/10 | 10/10 | 10/10 |
| 6 controls | 10/10 | 10/10 | 10/10 |

- **#1940 works with a real model on its own prompt.**
- **#1772 changed nothing for this model in these runs.** Its own measurement used a small model
  (deepseek); whether it helps there is not tested here.
- **In 10 runs, this model did not make #1728's error.** The reporter saw it with a small Gemini
  judge, which the `over_eager` persona reproduces. Ten runs of one model do not show that
  capable models never make it.
- **Cache misses.** Every #1772 and #1940 answer was a cache miss, consistent with both changing
  the prompt text (their diffs do). A miss alone does not prove a prompt change: model, effort,
  repetition and candidate content are part of the key too.
- **Old cache keys.** These answers were recorded under the first cache key, which could collide
  for the same text split differently into messages. They are kept as a record but no longer
  replay (DESIGN.md, mistake 11).

## Graphiti's open PR queue on 2026-10-04 (main at `5d47d4d`)

Each PR is merged onto current main, and each scenario runs 3 times. Results are in
`results/gate/triage-queue.md` and `results/gate/reach-queue.jsonl`.

| PR | what the gate shows | changed statements it ran |
|---|---|---|
| #1729 | fixes #1728 (unrelated facts retired, with an over-flagging judge); breaks nothing | 11/11 |
| #1940 | fixes #1841 (a release ends the assignment), also with a real model; breaks nothing | 22/24 |
| #1867 | fixes #1865 (a fact true again) and the expiry of facts that arrive ended | 26/31 |
| #1596 | fixes #488 (date filters with OR groups) | 2/8 |
| #1741 | fixes #1734 (an entity with the exact name, below the embedding threshold) | 7/8 |
| #1873 | fixes #1872 (one fact in two episodes of a bulk batch) | 9/10 |
| #1772 | no change with these judges, nor in 10 Codex runs per scenario; its own measurement used a small model | 25/28 |
| this repository's fix 1 | fixes history depth in all three paths | 9/9 |
| #1569, #1595 | no change: the code runs, but no scenario asserts escaping | 1/1 |
| #1914, #1671, #1912 | **untested**: hash-seed determinism, a test-only function, LLM-written summaries | 0/1, 0/2, 0/21 |
| #1626 | does not merge onto main (conflict in a FalkorDB test file) | |

Every mergeable PR fixes what it claims, and none breaks a covered behaviour (`results/gate/triage-final.md`,
3 runs per scenario, the evaluator that checks the whole stored graph).

"Ran" counts a changed statement as run only if it executed during the 29 scenarios. A changed
function counts only if its body ran, not merely because its module was imported, and changes
inside a multi-line statement such as a prompt string belong to that statement. "Ran" is not
"asserted": #1596's other lines handle date fields no scenario filters on.

**The temporal PRs complement each other.** The four temporal PRs each fix a different scenario,
so all of them could land, and none fixes history depth.

## When a bug started: running the gate on old commits

The gate runs on older Graphiti versions (`graphiti_gate/compat.py` bridges the API changes:
tracer, index setup, return tuples, the answer schema, and a prompt that lists the new fact first).

- **Run on three commits.** The commit before #906 (Sept 2025, which replaced an entity-scoped
  candidate query with a graph-wide search), #906 itself, and main all fail `depth-20` and pass
  the controls.
- **So #906 did not introduce the fault.** The scoped query was also capped at 10 with no time
  filter.
- **How far back it goes.** Versions before Graphiti's driver abstraction (March 2025) cannot
  run the gate.

## Shrinking a failure

`python -m graphiti_gate.shrink --trial N` removes facts one at a time from a failing random
history while the rule still fails on two consecutive runs.

The traced back-fill failure of fix 1 shrinks from 12 homes plus 4 of another person's facts to
8 homes plus those 4. That is 12 facts competing for 10 places, and removing any one makes the
failure vanish. That is the mechanism, read directly off the minimal case.

## Audit of a live graph

`graphiti-gate audit` reports, read-only:

- **two current values:** subjects with more than one open fact under one relation (candidates,
  since Graphiti does not record cardinality),
- **exposure:** subjects with 9 or more superseded facts in one relation, where the history-depth
  fault starts,
- **interval damage:** inverted and zero-length intervals,
- **ended but not expired:** facts with `invalid_at` set and no `expired_at`.

For each double-current candidate it prints the Cypher that would end the older fact. It never
runs it.

**Validation** (`bench/validate_audit.py`). We built 20 graphs through Graphiti's real resolver,
each with 20 past homes and one move.

| | graphs left with two current homes | flagged by the audit | false alarms | missed |
|---|---|---|---|---|
| without fix 1 | 12 of 20 | 12 | 0 | 0 |
| with fix 1 | 0 of 20 | 0 | 0 | 0 |

It also found a planted inverted interval and a planted ended-but-not-expired fact.

**What this validation covers, and what it does not.**

- **Covered.** One single-valued relation (home), in synthetic graphs. Ground truth is whether
  Graphiti's resolver returned the old home as retired, checked against what was persisted.
- **Not covered.** On real graphs, two open facts under one relation can be legitimate (likes,
  memberships); the audit cannot tell, which is why it calls them candidates. The zero-false-alarm
  result applies to these graphs only.

## In CI

`ci/graphiti-memory-gate.yml` is a drop-in workflow for Graphiti's repository. It runs on PRs
that touch resolution or search, uses the Neo4j version in their `unit_tests.yml`, and writes the
scorecard to the job summary. It never comments on a PR. Installed with `uv tool install`, the
gate scored a plain Graphiti checkout with fix 1 against main the same way it does in development
(3 fixed, 0 broken of the 24 scenarios it had then).
