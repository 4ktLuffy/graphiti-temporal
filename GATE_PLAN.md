# Plan: a behavioural gate for Graphiti's memory logic

**Status: approved 2026-10-04, revised after a self-review (below).**

## Revisions from self-review

1. **Judge personas, not only an oracle.** A perfect oracle never wrongly flags an unrelated fact,
   so a guard like #1729 would look useless. Scenarios declare a judge:
   - `oracle`: ground truth.
   - `over_eager`: flags every similar fact, which is #1728's failure.
   - `under_resolving`: returns nothing on about half the questions, as #1772 measured on
     deepseek.
2. **Format-agnostic oracle.** It matches facts by text and answers in whatever response schema
   the revision asks for, so it works on #1772's E/I ids and #1940's prompt. It is tested on #1772
   first.
3. **No ties.** A single scenario must be tie-free and give the same verdict 5 runs out of 5. Rates
   come only from sweeps, with intervals, because Neo4j tie-breaking made our own control read
   29/30.
4. **A PR's version is its head merged onto current main**, which is what would land. A conflict
   is reported as a conflict, not a failure.
5. **Mutation score, ours and theirs.** The same injected bugs run against Graphiti's existing unit
   tests and against the gate. Both catch rates are reported.
6. **The audit is validated on graphs built by real ingestion**: with the bug, where the damage is
   known, and with the fix, where there should be none. No claims are made about production graphs
   we have not seen.
7. **Scope.** Start where PRs compete (invalidation, intervals, duplicates); widen only after.
8. **Codex quota.** Quota or credit errors stop a run cleanly and say so, so Henos can reset.

## The problem it solves

Zep has about 10 people and 242 open PRs, 119 of them older than 90 days. Four PRs compete to fix
edge invalidation (#1729, #1772, #1940, #1867), and none has a maintainer review. Reviewing one
means reasoning through LLM-dependent temporal logic by hand.

- **What their CI covers.** It runs unit tests with mocked LLMs, plus DB tests with no model
  (`.github/workflows/unit_tests.yml:39,109-115`). No test starts from a graph, sends an episode
  in, and checks what the graph believes afterwards.
- **What is missing.** The only resolution evaluation, `tests/evals`, is ignored by CI and needs
  OpenAI.
- **The question nobody can answer cheaply.** "Does this PR fix what it claims, and what else
  does it change?"

The gate answers that in minutes, without API keys, for any git ref or PR.

## What it is

```
graphiti-gate run    --ref <sha|branch|pr:1729>        # all scenarios on one revision
graphiti-gate diff   --base main --head pr:1729        # the scorecard: fixed / broke / unchanged
graphiti-gate triage --prs 1729,1772,1940,1867,...     # the whole queue, one table
graphiti-gate audit  --neo4j bolt://...                # the same invariants on a live graph
graphiti-gate sweep  --ref main --depths 0..50         # history-depth curves with intervals
```

### 1. Scenarios from their own issues

Each scenario is a small YAML file. It holds:

- the **starting graph**: facts with dates,
- the **incoming input**: an episode, or extracted edges,
- **what the judge would rule**,
- the **expected graph afterwards**: which facts are open, which ended and when, which merged,
- the **source issue**, with a link.

The first set comes from the research (`scratchpad/gate_research.md`, 35 candidates):

| family | issues | example |
|---|---|---|
| history depth | ours | the live home is retired after 10, 20 or 50 past homes |
| over-invalidation | #1728 | `WORKS_AT` is not retired by an unrelated `ResponsibleFor` |
| end-only facts | #1841 | "released from Payments" closes "assigned to Payments" |
| re-assertion | #1865 | a fact true again after it ended becomes a new interval |
| back-fill | ours | an older fact added late is closed by the later fact |
| dedup determinism | #1916, #1931 | same winner under every hash seed |
| exact-name dedup | #1734 | "DGIA" merges even below the cosine floor |
| bulk provenance | #1872 | one edge, every episode recorded |
| filters | #488 | OR'd date filters keep both dates |
| known gaps | #1728 (additive) | marked expected-to-fail with a reason, so the gate shows when one is fixed |

### 2. Three ways to answer the model's questions

- **Oracle (default, CI).** The model is replaced by ground truth, as in this repository's
  `OracleLLM`. It reads the facts out of Graphiti's real prompt and answers in whatever schema the
  revision asks for, so it also works for PRs that change the answer format, like #1772's E/I
  ids. The result is deterministic and runs in seconds. It tests Graphiti's code: search,
  scoping, interval rules and merging.
- **Replay.** Real model answers are recorded once, keyed by a hash of the prompt. A PR that
  changes a prompt shows up as a cache miss, so the scorecard lists exactly which prompts it
  touched. Unchanged prompts replay for free.
- **Live (scheduled, not per PR).** Codex answers, repeated N times, with Wilson intervals and
  paired before/after tests on the same seeds. It is for PRs that change prompts, whose effect
  only exists as a rate.

### 3. Invariants checked on every run (metamorphic tests)

These need no hand-written expected outcome, so they scale to random histories:

- **No double-current.** No subject has two open facts in a slot after a contradiction.
- **Intervals only shrink.** An ending is never extended (#1940 and #1867 both assert this).
- **Back-fill order independence.** Ingesting the same history in any order gives the same
  intervals.
- **Idempotence.** Ingesting the same episode twice changes nothing.
- **Determinism.** The result is the same under any `PYTHONHASHSEED`.
- **Lossless pre-filters.** A pre-filter changes no decision, by the method of
  `tests/test_lossless.py`.

### 4. The scorecard

The output is Markdown a maintainer reads in two minutes:

- the scenarios fixed, broken and unchanged versus base,
- invariant violations,
- prompts touched,
- the change in LLM calls per episode,
- query-time regressions, so a 20-80× slowdown like our own patch 02's is caught.

It can be read locally or posted by their CI. We post nothing.

### 5. Is the gate itself trustworthy?

Two lessons from building fix 1 shape the gate:

- **Compare the whole edge.** The first "lossless" test compared only dates and retirements, and
  missed `expired_at` and attribute differences. Scenarios assert the full stored state of every
  touched edge.
- **A second model reviews each phase by running code** before its results are trusted. Two
  review rounds on fix 1 each found real errors.

Checks on the gate itself:

- **Negative controls.** Every scenario must fail on the revision that has the bug (main, for
  open bugs) and pass on a revision that fixes it.
- **Mutation testing.** Inject about 20 small mutations into Graphiti's resolution code, such as
  flipping a date comparison, dropping a filter or reversing a tie-break. The gate must catch
  them, and we report the mutation score.
- **Cross-check with reality.** For the history-depth family, oracle verdicts must match the
  real-model runs already in this repository: 0/12 vs 12/12 at depth 50.

### 6. The demo: their own queue

Run `triage` on every open PR that touches resolution or search: #1729, #1772, #1940, #1867,
#1914, #1873, #1741, #1912, #1596, plus ours. Each is applied onto the same main. The result is
one table showing which fixes do what they claim, which break other scenarios, and which conflict
with each other. Expected surprises:

- #1729 filters after search, so it likely does not help at depth.
- #1940 and #1867 may disagree on re-assertion.

All of this runs locally and is read-only. Nothing goes on their PRs without your yes.

### 7. Delivery for Zep

- **Package.** `pip install graphiti-gate`, a scenario directory and a CLI.
- **CI templates.** A GitHub Action file they can drop in, oracle on every PR at about 1 minute
  plus Neo4j, and a nightly live job. It is a file in our repo; we do not open a PR on theirs.
- **Audit.** `audit` for users with production graphs, using the same invariants plus "exposure"
  (subjects with 9 or more superseded facts in a slot).

## Phases and checkpoints

| # | phase | output | checkpoint |
|---|---|---|---|
| 1 | scenario format, runner on one revision, oracle mode, 8 scenarios | `run` on main: open bugs red, controls green | negative controls behave |
| 2 | `diff` and the scorecard, revision install via uv worktrees, PR fetch | scorecard for our fix 01 vs main | fix 01 shows only "fixed" |
| 3 | invariants and random histories, about 25 scenarios | invariant report on main | each violation traced to a known issue or a new finding |
| 4 | mutation testing | mutation score | at least 80% caught, or the gaps listed |
| 5 | `triage` on the open queue | the queue table | each surprising cell rechecked by hand |
| 6 | replay and live modes (Codex), cost and latency columns | live intervals for prompt-changing PRs | Codex budget agreed with you first |
| 7 | `audit`, CI templates, README, findings page | publishable repo | your review, then the email |

**Order of value.** Phases 1, 2, 3 and 5 are the core. If time runs short, 4 and 6 shrink.

## Risks

- **Wrong expected outcomes.** A scenario with a wrong expected outcome is worse than none.
  - Expected outcomes are taken from the issue text and labelled reporter-stated or
    maintainer-stated. Maintainers rarely state them.
  - Disputed cases are marked `disputed`. They are reported, never counted as pass or fail.
- **PR conflicts.** PRs conflict with each other (#1772, #1940, #1784, #1809 all edit
  `dedupe_edges.py`). Each PR is tested alone on the same main; combinations come later.
- **Prompt PRs are noisy.** Their real effect is a rate. Live mode costs Codex credits and gives
  intervals; small models already score 0/30 on contradictions (#1772's own data).
- **Tone.** A table that says someone's PR "breaks" something needs careful wording in the email:
  "differs from expectation on scenario X", with the scenario attached.

## Backlog: more capabilities once phases 1-7 are done

Requested by Henos on 2026-10-04: build more around the gate. These are candidates, to be planned
and challenged before building, as phases 1-7 were.

- **`bisect`.** Find the exact commit on main that changed a scenario's verdict. It would answer
  "when did this break?" automatically.
- **Shrinking.** When a random-history rule fails, reduce it to the smallest history that still
  fails, so the report is a 3-line case, not a 25-home trace.
- **Scenario from an issue.** Draft a scenario YAML from a GitHub issue's text (Codex-assisted).
  A human approves it before it counts.
- **Cross-backend matrix.** The same scenarios on FalkorDB, Kuzu and Neptune, where the search
  paths differ.
- **Performance and cost gate.** LLM calls and tokens per episode, query latency per scenario,
  and regressions flagged in the scorecard (our patch 02 lesson).
- **`audit` with repair plans.** Find damaged facts in a live graph, such as two current values or
  inverted intervals. Propose the exact edits for a human to apply.
- **Cross-system benchmark.** The same scenarios through adapters for Mem0, Cognee and Letta. It
  would be the first update-depth comparison of memory systems.
- **Live and replay model modes.** Real-model rates with intervals for prompt-changing PRs;
  recorded answers make reruns free.
- **Report page.** One page per triage run: the queue table, per-PR scorecards, traces of each
  failure.
